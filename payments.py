from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash, current_app
from flask_login import login_required, current_user
from models import db, User, Subscription, PLANS, app_is_free
from datetime import datetime, timedelta
from dotenv import load_dotenv
import stripe
import os

# Load environment variables first (override any stale shell values)
load_dotenv(override=True)

payments_bp = Blueprint('payments', __name__)

# Initialize Stripe - load key from environment
STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY')
STRIPE_PUBLISHABLE_KEY = os.getenv('STRIPE_PUBLISHABLE_KEY', '')

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    print("WARNING: STRIPE_SECRET_KEY not found in environment variables!")

# Plan prices (in cents) - moved to models.py but keeping here for backward compatibility
try:
    from models import PLAN_PRICES
except ImportError:
    PLAN_PRICES = {
        'free': 0,
        'basic': 499,  # $4.99
        'elite': 999,  # $9.99
        'advanced': 1999,  # $19.99
        'annual': 19999  # $199.99
    }

@payments_bp.route('/pricing')
def pricing():
    """Display pricing page"""
    # Get user's current plan if logged in
    current_plan_type = 'free'
    if current_user.is_authenticated:
        # Force complete refresh - expire all caches
        from sqlalchemy.orm import object_session
        session = object_session(current_user)
        if session:
            # Expire all cached data
            session.expire_all()
        
        # Get completely fresh user from database (bypassing cache)
        fresh_user = db.session.query(User).filter_by(id=current_user.id).first()
        
        # Force expire again to ensure fresh query
        if session:
            session.expire(fresh_user)
            session.expire_all()
        
        # Debug: Check all subscriptions first
        all_subs = db.session.query(Subscription).filter_by(user_id=fresh_user.id).all()
        print(f"Pricing page - User: {fresh_user.username} (ID: {fresh_user.id})")
        print(f"  All subscriptions in DB: {len(all_subs)}")
        for sub in all_subs:
            print(f"    - ID: {sub.id}, Plan: {sub.plan_type}, Status: {sub.status}, Updated: {sub.updated_at}, User ID: {sub.user_id}")
        
        active_subs = db.session.query(Subscription).filter(
            Subscription.user_id == fresh_user.id,
            Subscription.status == 'active'
        ).all()
        print(f"  Active subscriptions: {len(active_subs)}")
        for sub in active_subs:
            print(f"    - ID: {sub.id}, Plan: {sub.plan_type}, Status: {sub.status}")
        
        # Now get the plan - this should query fresh from DB
        current_plan_type = fresh_user.get_plan()
        
        # Debug: verify what we got
        active_sub = fresh_user.get_active_subscription()
        print(f"  Current plan (get_plan): {current_plan_type}")
        if active_sub:
            print(f"  get_active_subscription() returned: ID={active_sub.id}, Plan={active_sub.plan_type}, Status={active_sub.status}")
        else:
            print(f"  get_active_subscription() returned: None")
    
    return render_template('pricing.html', 
                         stripe_publishable_key=STRIPE_PUBLISHABLE_KEY,
                         plans=PLANS,
                         current_plan_type=current_plan_type,
                         app_is_free=app_is_free())

@payments_bp.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    """Create Stripe checkout session for subscription"""
    # Don't charge anyone while the admin has the app price set to 0.
    if app_is_free():
        return jsonify({
            'error': 'The app is currently free for all users - no subscription is needed.',
            'success': False
        }), 400

    try:
        # Verify Stripe API key is set
        if not stripe.api_key:
            # Try to reload from environment
            load_dotenv()
            stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
            if not stripe.api_key:
                return jsonify({
                    'error': 'Stripe API key not configured. Please contact support.',
                    'success': False
                }), 500
        
        plan_type = request.json.get('plan_type', 'basic')
        
        if plan_type == 'free':
            # Free plan doesn't need payment
            return jsonify({'error': 'Free plan does not require payment', 'success': False}), 400
        
        if plan_type not in PLANS:
            return jsonify({'error': 'Invalid plan type', 'success': False}), 400
        
        plan = PLANS[plan_type]
        price_cents = PLAN_PRICES.get(plan_type, 0)
        
        if price_cents == 0:
            return jsonify({'error': 'This plan does not require payment', 'success': False}), 400
        
        # Get or create Stripe customer
        current_subscription = current_user.get_active_subscription()
        customer_id = _get_or_create_stripe_customer(current_subscription)
        
        # Determine interval
        interval = 'month' if plan['interval'] == 'month' else 'year'
        
        # Create checkout session
        checkout_session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f'AI Article Generator - {plan["name"]} Plan',
                        'description': _get_plan_description(plan_type)
                    },
                    'recurring': {
                        'interval': interval
                    },
                    'unit_amount': price_cents,
                },
                'quantity': 1,
            }],
            mode='subscription',
            success_url=request.host_url + 'payment/success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.host_url + 'payment/cancel',
            metadata={'user_id': str(current_user.id), 'plan_type': plan_type}
        )
        
        return jsonify({
            'success': True,
            'sessionId': checkout_session.id,
            'url': checkout_session.url
        })
    
    except Exception as e:
        return jsonify({'error': str(e), 'success': False}), 500

def _get_plan_description(plan_type):
    """Get description for plan"""
    plan = PLANS.get(plan_type, {})
    if plan.get('queries_per_day'):
        return f'{plan["queries_per_day"]} queries per day'
    elif plan.get('queries_per_week'):
        return f'{plan["queries_per_week"]} queries per week'
    elif plan.get('queries_per_month'):
        return f'{plan["queries_per_month"]} queries per month'
    return 'Unlimited queries'

@payments_bp.route('/success')
@login_required
def payment_success():
    """
    Handle successful payment and update user's subscription.
    
    Process:
    1. Validate payment session
    2. Cancel old active subscriptions
    3. Create/update new subscription
    4. Verify plan is updated correctly
    5. Redirect to dashboard
    """
    print("=" * 80)
    print("PAYMENT_SUCCESS: Starting payment success handler")
    print("=" * 80)
    
    # Check if user is authenticated
    if not current_user.is_authenticated:
        print(f"  - ERROR: User not authenticated, redirecting to login")
        flash('Please log in to complete your payment', 'warning')
        return redirect(url_for('auth.login'))
    
    print(f"  - User authenticated: {current_user.username} (ID: {current_user.id})")
    
    session_id = request.args.get('session_id')
    print(f"STEP 1: Getting session_id from request")
    print(f"  - Session ID: {session_id}")
    print(f"  - Full request args: {dict(request.args)}")
    
    if not session_id:
        print(f"  - ERROR: No session_id provided, redirecting to dashboard with error")
        from urllib.parse import quote
        import time
        error_msg = 'Payment session not found. Please ensure you completed the payment process. If you were redirected here after payment, the session may have expired. Please try again or contact support.'
        dashboard_url = url_for('payments.dashboard')
        redirect_url = f'{dashboard_url}?payment_status=error&payment_error={quote("No session ID")}&payment_message={quote(error_msg)}&error={int(time.time())}'
        flash(error_msg, 'error')
        return redirect(redirect_url)
    
    try:
        # Step 1: Validate and retrieve payment information
        print(f"STEP 2: Validating payment session")
        session_data = _validate_payment_session(session_id)
        if not session_data:
            print(f"  - ERROR: Session validation failed, redirecting to dashboard with error")
            # Get error message from flash if available, or use default
            from urllib.parse import quote
            import time
            error_msg = 'Payment session validation failed. Please ensure you completed the payment and try again. If the issue persists, contact support.'
            dashboard_url = url_for('payments.dashboard')
            redirect_url = f'{dashboard_url}?payment_status=error&payment_error={quote("Session validation failed")}&payment_message={quote(error_msg)}&error={int(time.time())}'
            return redirect(redirect_url)
        
        plan_type = session_data['plan_type']
        subscription_id = session_data['subscription_id']
        stripe_subscription = session_data['stripe_subscription']
        unit_amount = session_data['unit_amount']
        
        print(f"  - SUCCESS: Session validated")
        print(f"    - Plan type: {plan_type}")
        print(f"    - Subscription ID: {subscription_id}")
        print(f"    - Unit amount: {unit_amount}")
        
        # Step 2: Cancel all old active subscriptions
        print(f"STEP 3: Canceling old active subscriptions")
        _cancel_old_subscriptions(subscription_id, plan_type)
        print(f"  - SUCCESS: Old subscriptions canceled")
        
        # Step 3: Create or update subscription
        print(f"STEP 4: Creating or updating subscription")
        subscription = _create_or_update_subscription(
            subscription_id=subscription_id,
            plan_type=plan_type,
            stripe_subscription=stripe_subscription,
            unit_amount=unit_amount
        )
        print(f"  - SUCCESS: Subscription created/updated")
        print(f"    - Subscription ID: {subscription.id}")
        print(f"    - Plan type: {subscription.plan_type}")
        print(f"    - Status: {subscription.status}")
        
        # Step 4: Verify plan is updated correctly
        print(f"STEP 5: Verifying plan update")
        _verify_plan_update(plan_type, subscription)
        print(f"  - SUCCESS: Plan verification completed")
        
        # Step 5: Final verification before redirect
        print(f"STEP 6: Final verification before redirect")
        # Force one more refresh to ensure everything is committed
        db.session.commit()
        from sqlalchemy.orm import object_session
        session_obj = object_session(current_user)
        if session_obj:
            session_obj.expire_all()
        
        # Get final fresh user to verify plan
        final_user = db.session.query(User).filter_by(id=current_user.id).first()
        final_plan = final_user.get_plan()
        print(f"  - Final user plan: {final_plan} (expected: {plan_type})")
        
        if final_plan != plan_type:
            print(f"  - WARNING: Plan mismatch before redirect! Expected {plan_type}, got {final_plan}")
            # Try one more time to fix it
            active_subs = db.session.query(Subscription).filter(
                Subscription.user_id == current_user.id,
                Subscription.status == 'active'
            ).order_by(Subscription.updated_at.desc()).all()
            
            if len(active_subs) > 0:
                newest_sub = active_subs[0]
                if newest_sub.plan_type != plan_type:
                    print(f"  - FIXING: Setting newest subscription plan_type to {plan_type}")
                    newest_sub.plan_type = plan_type
                    newest_sub.status = 'active'
                    newest_sub.updated_at = datetime.utcnow()
                    db.session.commit()
                    
                    # Re-verify
                    if session_obj:
                        session_obj.expire_all()
                    final_user = db.session.query(User).filter_by(id=current_user.id).first()
                    final_plan = final_user.get_plan()
                    print(f"  - After fix: Plan is now {final_plan}")
        else:
            print(f"  - SUCCESS: Plan matches expected value")
        
        # Step 6: Redirect to dashboard with success message
        print(f"STEP 7: Preparing redirect to dashboard")
        plan_name = PLANS.get(plan_type, {}).get('name', plan_type)
        success_message = f'Subscription activated successfully! Your plan has been updated to {plan_name}.'
        flash(success_message, 'success')
        
        import time
        from urllib.parse import quote
        dashboard_url = url_for('payments.dashboard')
        redirect_url = f'{dashboard_url}?updated={int(time.time())}&payment_status=success&payment_message={quote(success_message)}'
        print(f"  - Redirect URL: {redirect_url}")
        print(f"  - SUCCESS: Redirecting to dashboard")
        print("=" * 80)
        
        return redirect(redirect_url)
    
    except Exception as e:
        print(f"PAYMENT_SUCCESS: EXCEPTION - {e}")
        import traceback
        traceback.print_exc()
        
        # Try to recover: if we have a session_id, try to get plan info from Stripe directly
        if session_id:
            print(f"  - Attempting recovery: trying to get plan from Stripe session")
            try:
                session = stripe.checkout.Session.retrieve(session_id)
                if session.subscription:
                    stripe_sub = stripe.Subscription.retrieve(session.subscription)
                    plan_type = session.metadata.get('plan_type') if session.metadata else None
                    
                    if plan_type:
                        print(f"  - Recovery: Found plan_type {plan_type} from Stripe, attempting to update")
                        # Try to update subscription directly
                        subscription = db.session.query(Subscription).filter_by(
                            stripe_subscription_id=session.subscription
                        ).first()
                        
                        if subscription:
                            subscription.plan_type = plan_type
                            subscription.status = 'active'
                            subscription.updated_at = datetime.utcnow()
                            db.session.commit()
                            print(f"  - Recovery: Updated subscription plan to {plan_type}")
                            flash(f'Payment processed. Your plan has been updated to {PLANS.get(plan_type, {}).get("name", plan_type)}.', 'success')
                            import time
                            return redirect(f'{url_for("payments.dashboard")}?updated={int(time.time())}')
            except Exception as recovery_error:
                print(f"  - Recovery failed: {recovery_error}")
        
        error_message = f'Error processing payment: {str(e)}. Please check your dashboard or contact support.'
        flash(error_message, 'error')
        
        import time
        from urllib.parse import quote
        # Redirect to dashboard anyway so user can see their current status
        dashboard_url = url_for('payments.dashboard')
        redirect_url = f'{dashboard_url}?error={int(time.time())}&payment_status=error&payment_error={quote(str(e))}&payment_message={quote(error_message)}'
        print(f"  - Redirecting to dashboard with error: {redirect_url}")
        return redirect(redirect_url)


def _validate_payment_session(session_id):
    """Validate payment session and extract subscription data"""
    print(f"      STEP 2.1: Retrieving Stripe checkout session")
    try:
        # Retrieve checkout session
        session = stripe.checkout.Session.retrieve(session_id)
        print(f"        - Session retrieved: {session.id}")
        print(f"        - Session metadata: {session.metadata}")
        
        print(f"      STEP 2.2: Verifying session belongs to current user")
        # Verify it belongs to current user
        if not session.metadata:
            print(f"        - ERROR: No metadata in session")
            flash('Invalid payment session - no metadata found', 'error')
            return None
        
        session_user_id = session.metadata.get('user_id')
        current_user_id = str(current_user.id)
        print(f"        - Session user_id: {session_user_id}")
        print(f"        - Current user_id: {current_user_id}")
        
        if session_user_id != current_user_id:
            print(f"        - ERROR: User ID mismatch!")
            flash('Invalid payment session - user mismatch', 'error')
            return None
        
        print(f"        - SUCCESS: User ID matches")
        
        print(f"      STEP 2.3: Getting subscription ID from session")
        # Get subscription ID
        subscription_id = session.subscription
        if not subscription_id:
            print(f"        - ERROR: No subscription ID in session")
            error_msg = 'Invalid payment session - no subscription found. The payment may still be processing. Please wait a moment and check your dashboard.'
            flash(error_msg, 'error')
            return None
        
        print(f"        - Subscription ID: {subscription_id}")
        
        print(f"      STEP 2.4: Retrieving Stripe subscription")
        # Retrieve Stripe subscription
        stripe_subscription = stripe.Subscription.retrieve(subscription_id)
        print(f"        - Stripe subscription retrieved: {stripe_subscription.id}")
        print(f"        - Status: {stripe_subscription.status}")
        
        print(f"      STEP 2.5: Getting plan type from metadata")
        # Get plan type from metadata
        plan_type = session.metadata.get('plan_type', 'basic')
        print(f"        - Plan type: {plan_type}")
        
        print(f"      STEP 2.6: Extracting subscription price")
        # Get price from subscription items
        unit_amount = _extract_subscription_price(stripe_subscription)
        if unit_amount is None:
            print(f"        - WARNING: Could not extract price, subscription may be processing")
            info_msg = 'Subscription is being processed. Please check your dashboard in a moment. If the issue persists, contact support.'
            flash(info_msg, 'info')
            return None
        
        print(f"        - Unit amount: {unit_amount}")
        print(f"      - SUCCESS: Session validation complete")
        
        return {
            'plan_type': plan_type,
            'subscription_id': subscription_id,
            'stripe_subscription': stripe_subscription,
            'unit_amount': unit_amount
        }
    
    except stripe.error.StripeError as e:
        print(f"      - ERROR: Stripe error validating payment session: {e}")
        flash(f'Stripe error: {str(e)}', 'error')
        return None
    except Exception as e:
        print(f"      - ERROR: Exception validating payment session: {e}")
        import traceback
        traceback.print_exc()
        flash(f'Error validating payment session: {str(e)}', 'error')
        return None


def _extract_subscription_price(stripe_subscription):
    """Extract price from Stripe subscription items"""
    if not hasattr(stripe_subscription, 'items') or not stripe_subscription.items:
        return None
    
    # Handle both dict and object access
    if isinstance(stripe_subscription.items, dict):
        items_data = stripe_subscription.items.get('data', [])
    else:
        items_data = stripe_subscription.items.data if hasattr(stripe_subscription.items, 'data') else []
    
    if not items_data or len(items_data) == 0:
        return None
    
    # Get price from first item
    first_item = items_data[0]
    if isinstance(first_item, dict):
        price_data = first_item.get('price', {})
        return price_data.get('unit_amount', 0) if isinstance(price_data, dict) else 0
    else:
        price_obj = first_item.price if hasattr(first_item, 'price') else None
        return price_obj.unit_amount if price_obj and hasattr(price_obj, 'unit_amount') else 0


def _cancel_old_subscriptions(new_subscription_id, new_plan_type):
    """Cancel all existing active subscriptions except the new one"""
    print(f"    STEP 3.1: Querying for old active subscriptions")
    old_subscriptions = db.session.query(Subscription).filter(
        Subscription.user_id == current_user.id,
        Subscription.status == 'active'
    ).all()
    
    print(f"    - Found {len(old_subscriptions)} active subscription(s)")
    for sub in old_subscriptions:
        print(f"      - ID: {sub.id}, Plan: {sub.plan_type}, Stripe ID: {sub.stripe_subscription_id}")
    
    if not old_subscriptions:
        print(f"    - No old subscriptions to cancel")
        return
    
    now = datetime.utcnow()
    canceled_count = 0
    
    print(f"    STEP 3.2: Processing each old subscription")
    for old_sub in old_subscriptions:
        # Skip if this is the subscription we're about to update
        if old_sub.stripe_subscription_id == new_subscription_id and old_sub.plan_type == new_plan_type:
            print(f"      - Skipping subscription {old_sub.id} (this is the new one)")
            continue
        
        print(f"      - Canceling subscription {old_sub.id} (Plan: {old_sub.plan_type})")
        
        # Cancel subscription in Stripe if it has a Stripe ID
        if old_sub.stripe_subscription_id:
            try:
                print(f"        - Canceling in Stripe: {old_sub.stripe_subscription_id}")
                stripe.Subscription.modify(
                    old_sub.stripe_subscription_id,
                    cancel_at_period_end=True
                )
                print(f"        - SUCCESS: Canceled in Stripe")
            except Exception as e:
                print(f"        - WARNING: Failed to cancel in Stripe: {e}")
        
        # Mark as canceled in database
        print(f"        - Marking as canceled in database")
        old_sub.status = 'canceled'
        old_sub.updated_at = now
        canceled_count += 1
        print(f"        - SUCCESS: Marked as canceled")
    
    if canceled_count > 0:
        print(f"    STEP 3.3: Committing cancellations to database")
        db.session.commit()
        print(f"      - SUCCESS: Committed {canceled_count} cancellation(s)")
    else:
        print(f"    - No subscriptions were canceled")


def _create_or_update_subscription(subscription_id, plan_type, stripe_subscription, unit_amount):
    """Create or update subscription in database"""
    print(f"    STEP 4.1: Checking for existing subscription")
    now = datetime.utcnow()
    
    # Try to find existing subscription by Stripe ID
    subscription = db.session.query(Subscription).filter_by(
        stripe_subscription_id=subscription_id
    ).first()
    
    if subscription:
        print(f"      - Found existing subscription (ID: {subscription.id})")
        print(f"        Before update: plan_type={subscription.plan_type}, status={subscription.status}")
        # Update existing subscription
        subscription.status = 'active'
        subscription.plan_type = plan_type
        subscription.stripe_customer_id = _get_stripe_customer_id(stripe_subscription, subscription.stripe_customer_id)
        subscription.amount = unit_amount / 100
        subscription.currency = _get_stripe_currency(stripe_subscription)
        subscription.current_period_start = _get_period_start(stripe_subscription, subscription.current_period_start)
        subscription.current_period_end = _get_period_end(stripe_subscription, now)
        subscription.updated_at = now
        print(f"        After update: plan_type={subscription.plan_type}, status={subscription.status}")
    else:
        print(f"      - No existing subscription found, creating new one")
        # Create new subscription
        subscription = Subscription(
            user_id=current_user.id,
            stripe_subscription_id=subscription_id,
            stripe_customer_id=_get_stripe_customer_id(stripe_subscription, None),
            status='active',
            plan_type=plan_type,
            amount=unit_amount / 100,
            currency=_get_stripe_currency(stripe_subscription),
            current_period_start=_get_period_start(stripe_subscription, now),
            current_period_end=_get_period_end(stripe_subscription, now),
            updated_at=now
        )
        db.session.add(subscription)
        print(f"      - New subscription created (ID: {subscription.id})")
    
    # Ensure subscription is active and has correct plan_type
    print(f"    STEP 4.2: Ensuring subscription is active with correct plan_type")
    subscription.status = 'active'
    subscription.plan_type = plan_type
    subscription.updated_at = now
    print(f"      - Status: {subscription.status}, Plan: {subscription.plan_type}")
    
    # Commit changes
    print(f"    STEP 4.3: Committing subscription to database")
    db.session.commit()
    print(f"      - SUCCESS: Committed to database")
    
    print(f"    STEP 4.4: Refreshing subscription from database")
    db.session.refresh(subscription)
    print(f"      - Refreshed: ID={subscription.id}, Plan={subscription.plan_type}, Status={subscription.status}")
    
    return subscription


def _get_stripe_customer_id(stripe_subscription, fallback=None):
    """Extract customer ID from Stripe subscription"""
    if hasattr(stripe_subscription, 'customer'):
        return stripe_subscription.customer
    return fallback


def _get_stripe_currency(stripe_subscription):
    """Extract currency from Stripe subscription"""
    if hasattr(stripe_subscription, 'currency'):
        return stripe_subscription.currency.upper()
    return 'USD'


def _get_period_start(stripe_subscription, fallback):
    """Extract period start from Stripe subscription"""
    if hasattr(stripe_subscription, 'current_period_start'):
        return datetime.fromtimestamp(stripe_subscription.current_period_start)
    return fallback


def _get_period_end(stripe_subscription, fallback):
    """Extract period end from Stripe subscription"""
    if hasattr(stripe_subscription, 'current_period_end'):
        return datetime.fromtimestamp(stripe_subscription.current_period_end)
    return fallback + timedelta(days=30)


def _verify_plan_update(expected_plan_type, subscription):
    """Verify that the user's plan has been updated correctly"""
    print(f"    STEP 5.1: Forcing refresh of user data")
    # Force refresh user data
    from sqlalchemy.orm import object_session
    session_obj = object_session(current_user)
    if session_obj:
        session_obj.expire_all()
        print(f"      - Expired all cached data")
    
    print(f"    STEP 5.2: Querying fresh user from database")
    # Get fresh user and verify plan
    fresh_user = db.session.query(User).filter_by(id=current_user.id).first()
    print(f"      - Fresh user retrieved: {fresh_user.username} (ID: {fresh_user.id})")
    
    print(f"    STEP 5.3: Getting user's current plan")
    actual_plan = fresh_user.get_plan()
    print(f"      - Actual plan: {actual_plan}")
    print(f"      - Expected plan: {expected_plan_type}")
    
    if actual_plan != expected_plan_type:
        print(f"    STEP 5.4: WARNING - Plan mismatch detected!")
        print(f"      - Expected: {expected_plan_type}")
        print(f"      - Actual: {actual_plan}")
        
        # Check for multiple active subscriptions
        print(f"    STEP 5.5: Checking for multiple active subscriptions")
        active_subs = db.session.query(Subscription).filter(
            Subscription.user_id == current_user.id,
            Subscription.status == 'active'
        ).order_by(Subscription.updated_at.desc()).all()
        
        print(f"      - Found {len(active_subs)} active subscription(s):")
        for sub in active_subs:
            print(f"        - ID: {sub.id}, Plan: {sub.plan_type}, Updated: {sub.updated_at}")
        
        if len(active_subs) > 1:
            print(f"      - Multiple active subscriptions found, canceling duplicates")
            # Cancel all except the newest (most recent)
            for sub in active_subs[1:]:
                print(f"        - Canceling subscription {sub.id} (Plan: {sub.plan_type})")
                sub.status = 'canceled'
                sub.updated_at = datetime.utcnow()
            db.session.commit()
            print(f"      - SUCCESS: Duplicates canceled")
            
            # Re-verify
            print(f"    STEP 5.6: Re-verifying after canceling duplicates")
            if session_obj:
                session_obj.expire_all()
            fresh_user = db.session.query(User).filter_by(id=current_user.id).first()
            actual_plan = fresh_user.get_plan()
            print(f"      - Plan after cleanup: {actual_plan}")
        
        # Final check - ensure subscription is active
        print(f"    STEP 5.7: Final check - ensuring subscription is active")
        if subscription.status != 'active':
            print(f"      - Subscription status is '{subscription.status}', fixing to 'active'")
            subscription.status = 'active'
            subscription.updated_at = datetime.utcnow()
            db.session.commit()
            print(f"      - SUCCESS: Subscription status fixed")
        else:
            print(f"      - Subscription status is already 'active'")
    else:
        print(f"    STEP 5.4: SUCCESS - Plan matches expected value")
    
    print(f"    FINAL RESULT: User plan is '{actual_plan}' (expected: '{expected_plan_type}')")

@payments_bp.route('/cancel')
@login_required
def payment_cancel():
    """Handle cancelled payment"""
    flash('Payment was cancelled', 'info')
    return redirect(url_for('payments.pricing'))

@payments_bp.route('/dashboard')
@login_required
def dashboard():
    """User subscription dashboard"""
    # Force complete refresh - expire all caches
    from sqlalchemy.orm import object_session
    from flask import make_response
    
    # CRITICAL: Force Flask-Login to reload user from database
    # This ensures we have the latest subscription data
    user_id = current_user.id
    session = object_session(current_user)
    if session:
        # Expire all cached data
        session.expire_all()
    
    # Get completely fresh user from database (bypassing all caches)
    fresh_user = db.session.query(User).filter_by(id=user_id).first()
    
    # Force expire again to ensure fresh query
    if session:
        session.expire(fresh_user)
        session.expire_all()
    
    # Debug: Check all subscriptions first
    all_subs = db.session.query(Subscription).filter_by(user_id=fresh_user.id).all()
    print(f"Dashboard - User: {fresh_user.username} (ID: {fresh_user.id})")
    print(f"  All subscriptions in DB: {len(all_subs)}")
    for sub in all_subs:
        print(f"    - ID: {sub.id}, Plan: {sub.plan_type}, Status: {sub.status}, Updated: {sub.updated_at}, User ID: {sub.user_id}")
    
    active_subs = db.session.query(Subscription).filter(
        Subscription.user_id == fresh_user.id,
        Subscription.status == 'active'
    ).all()
    print(f"  Active subscriptions: {len(active_subs)}")
    for sub in active_subs:
        print(f"    - ID: {sub.id}, Plan: {sub.plan_type}, Status: {sub.status}")
    
    # Now get the subscription and plan - this should query fresh from DB
    subscription = fresh_user.get_active_subscription()
    plan_type = fresh_user.get_plan()
    plan = PLANS.get(plan_type, PLANS['free'])
    remaining_queries = fresh_user.get_remaining_queries()
    
    # Debug: verify what we got
    print(f"  Current plan (get_plan): {plan_type}")
    if subscription:
        print(f"  get_active_subscription() returned: ID={subscription.id}, Plan={subscription.plan_type}, Status={subscription.status}")
    else:
        print(f"  get_active_subscription() returned: None")
    
    # Create response with no-cache headers to prevent browser caching
    response = make_response(render_template('dashboard.html', 
                         subscription=subscription,
                         user=fresh_user,
                         plan=plan,
                         plan_type=plan_type,
                         remaining_queries=remaining_queries,
                         all_plans=PLANS))
    
    # Add cache-control headers to prevent browser from caching the dashboard
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response

@payments_bp.route('/change-plan', methods=['POST'])
@login_required
def change_plan():
    """
    Change user's subscription plan.
    
    Process:
    1. Validate new plan type
    2. If switching to free: update subscription locally (no payment needed)
    3. If switching to paid plan: redirect to Stripe checkout (payment required)
    """
    print("=" * 80)
    print("CHANGE_PLAN: Starting plan change process")
    print("=" * 80)

    # Don't charge anyone while the admin has the app price set to 0.
    if app_is_free():
        return jsonify({
            'error': 'The app is currently free for all users - no plan change is needed.',
            'success': False
        }), 400
    
    try:
        # Step 1: Validate input
        print(f"STEP 1: Validating input")
        data = request.json
        new_plan_type = data.get('plan_type')
        print(f"  - Received plan_type: {new_plan_type}")
        
        if not new_plan_type or new_plan_type not in PLANS:
            print(f"  - ERROR: Invalid plan type")
            return jsonify({'error': 'Invalid plan type', 'success': False}), 400
        
        print(f"  - SUCCESS: Plan type is valid")
        
        # Step 2: Check if already on this plan
        print(f"STEP 2: Checking current plan")
        current_plan_type = current_user.get_plan()
        print(f"  - Current plan: {current_plan_type}")
        print(f"  - New plan: {new_plan_type}")
        
        if new_plan_type == current_plan_type:
            print(f"  - ERROR: Already on this plan")
            return jsonify({'error': 'You are already on this plan', 'success': False}), 400
        
        print(f"  - SUCCESS: Plan change is valid")
        
        new_plan = PLANS[new_plan_type]
        current_subscription = current_user.get_active_subscription()
        print(f"  - Current subscription: {current_subscription.id if current_subscription else 'None'}")
        
        # Step 3: Handle free plan (no payment required)
        if new_plan_type == 'free':
            print(f"STEP 3: Switching to FREE plan (no payment required)")
            return _switch_to_free_plan(current_subscription)
        
        # Step 4: Handle paid plan (requires Stripe checkout)
        print(f"STEP 4: Switching to PAID plan (requires Stripe checkout)")
        return _switch_to_paid_plan(new_plan_type, new_plan, current_subscription)
    
    except Exception as e:
        print(f"CHANGE_PLAN: EXCEPTION - {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'success': False}), 500


def _switch_to_free_plan(current_subscription):
    """Switch user to free plan - no payment required"""
    print(f"  STEP 3.1: Canceling Stripe subscription (if exists)")
    # Cancel Stripe subscription if exists
    if current_subscription and current_subscription.stripe_subscription_id:
        try:
            print(f"    - Canceling Stripe subscription: {current_subscription.stripe_subscription_id}")
            stripe.Subscription.modify(
                current_subscription.stripe_subscription_id,
                cancel_at_period_end=True
            )
            print(f"    - SUCCESS: Stripe subscription canceled")
        except Exception as e:
            print(f"    - WARNING: Failed to cancel Stripe subscription: {e}")
            # Continue with local update even if Stripe fails
    else:
        print(f"    - No Stripe subscription to cancel")
    
    print(f"  STEP 3.2: Updating subscription in database")
    # Update existing subscription or create new one
    now = datetime.utcnow()
    if current_subscription:
        print(f"    - Updating existing subscription (ID: {current_subscription.id})")
        print(f"      Before: plan_type={current_subscription.plan_type}, status={current_subscription.status}")
        current_subscription.plan_type = 'free'
        current_subscription.status = 'active'
        current_subscription.amount = 0
        current_subscription.updated_at = now
        print(f"      After: plan_type={current_subscription.plan_type}, status={current_subscription.status}")
    else:
        print(f"    - Creating new free subscription")
        new_subscription = Subscription(
            user_id=current_user.id,
            plan_type='free',
            status='active',
            amount=0,
            current_period_start=now,
            current_period_end=now + timedelta(days=365)
        )
        db.session.add(new_subscription)
        print(f"    - New subscription created (ID: {new_subscription.id})")
    
    print(f"  STEP 3.3: Committing changes to database")
    db.session.commit()
    print(f"    - SUCCESS: Database committed")
    
    print(f"  STEP 3.4: Verifying plan update")
    # Force refresh and verify
    from sqlalchemy.orm import object_session
    session = object_session(current_user)
    if session:
        session.expire_all()
    
    fresh_user = db.session.query(User).filter_by(id=current_user.id).first()
    actual_plan = fresh_user.get_plan()
    print(f"    - User plan after update: {actual_plan} (expected: free)")
    
    if actual_plan == 'free':
        print(f"    - SUCCESS: Plan updated correctly")
    else:
        print(f"    - ERROR: Plan mismatch! Expected 'free', got '{actual_plan}'")
    
    print(f"  STEP 3.5: Returning success response")
    return jsonify({
        'success': True,
        'message': 'Plan changed to Free successfully!',
        'requires_payment': False
    })


def _switch_to_paid_plan(new_plan_type, new_plan, current_subscription):
    """Switch user to paid plan - requires Stripe checkout"""
    print(f"  STEP 4.1: Getting or creating Stripe customer")
    try:
        # Get or create Stripe customer
        customer_id = _get_or_create_stripe_customer(current_subscription)
        print(f"    - Stripe customer ID: {customer_id}")
        
        print(f"  STEP 4.2: Creating Stripe checkout session")
        # Create Stripe checkout session
        checkout_session = _create_checkout_session(
            customer_id=customer_id,
            plan_type=new_plan_type,
            plan=new_plan
        )
        print(f"    - Checkout session created: {checkout_session.id}")
        print(f"    - Checkout URL: {checkout_session.url}")
        
        print(f"  STEP 4.3: Returning checkout URL to frontend")
        print(f"    - SUCCESS: User will be redirected to Stripe checkout")
        print(f"    - NOTE: Plan will be updated after payment in payment_success()")
        
        return jsonify({
            'success': True,
            'requires_payment': True,
            'checkout_url': checkout_session.url,
            'message': 'Redirecting to payment...'
        })
    
    except Exception as e:
        print(f"  STEP 4.ERROR: Exception creating checkout session: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'error': f'Error creating checkout session: {str(e)}',
            'success': False
        }), 500


def _get_or_create_stripe_customer(current_subscription):
    """Get existing Stripe customer ID or create a new one"""
    if current_subscription and current_subscription.stripe_customer_id:
        return current_subscription.stripe_customer_id
    
    # Create new Stripe customer
    customer = stripe.Customer.create(
        email=current_user.email,
        metadata={
            'user_id': str(current_user.id),
            'username': current_user.username
        }
    )
    return customer.id


def _create_checkout_session(customer_id, plan_type, plan):
    """Create Stripe checkout session for the new plan"""
    interval = 'month' if plan['interval'] == 'month' else 'year'
    price_cents = PLAN_PRICES.get(plan_type, 0)
    
    return stripe.checkout.Session.create(
        customer=customer_id,
        payment_method_types=['card'],
        line_items=[{
            'price_data': {
                'currency': 'usd',
                'product_data': {
                    'name': f'AI Article Generator - {plan["name"]} Plan',
                    'description': _get_plan_description(plan_type)
                },
                'recurring': {
                    'interval': interval
                },
                'unit_amount': price_cents,
            },
            'quantity': 1,
        }],
        mode='subscription',
        success_url=request.host_url + 'payment/success?session_id={CHECKOUT_SESSION_ID}',
        cancel_url=request.host_url + 'payment/dashboard',
        metadata={
            'user_id': str(current_user.id),
            'plan_type': plan_type,
            'is_plan_change': 'true'
        }
    )

@payments_bp.route('/cancel-subscription', methods=['POST'])
@login_required
def cancel_subscription():
    """Cancel user subscription"""
    try:
        subscription = current_user.get_active_subscription()
        
        if not subscription:
            return jsonify({'error': 'No active subscription found', 'success': False}), 404
        
        # Cancel in Stripe if it has a Stripe subscription ID
        if subscription.stripe_subscription_id:
            try:
                stripe.Subscription.modify(
                    subscription.stripe_subscription_id,
                    cancel_at_period_end=True
                )
            except Exception as e:
                print(f"Warning: Failed to cancel Stripe subscription: {e}")
                # Continue with local update even if Stripe fails
        
        # Mark as canceled in database
        subscription.status = 'canceled'
        subscription.updated_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Subscription will be cancelled at the end of the current period'
        })
    
    except Exception as e:
        print(f"Error canceling subscription: {e}")
        return jsonify({'error': str(e), 'success': False}), 500

@payments_bp.route('/webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhooks for subscription events"""
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    webhook_secret = os.getenv('STRIPE_WEBHOOK_SECRET')
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
    except ValueError:
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({'error': 'Invalid signature'}), 400
    
    # Handle the event
    if event['type'] == 'customer.subscription.updated':
        subscription_obj = event['data']['object']
        _update_subscription_from_stripe(subscription_obj)
    elif event['type'] == 'customer.subscription.deleted':
        subscription_obj = event['data']['object']
        _delete_subscription_from_stripe(subscription_obj)
    
    return jsonify({'status': 'success'}), 200

def _update_subscription_from_stripe(stripe_subscription):
    """Update subscription in database from Stripe webhook"""
    try:
        subscription = db.session.query(Subscription).filter_by(
            stripe_subscription_id=stripe_subscription['id']
        ).first()
        
        if subscription:
            subscription.status = stripe_subscription['status']
            subscription.current_period_start = datetime.fromtimestamp(stripe_subscription['current_period_start'])
            subscription.current_period_end = datetime.fromtimestamp(stripe_subscription['current_period_end'])
            subscription.updated_at = datetime.utcnow()
            db.session.commit()
    except Exception as e:
        print(f"Error updating subscription from webhook: {e}")


def _delete_subscription_from_stripe(stripe_subscription):
    """Delete subscription from database from Stripe webhook"""
    try:
        subscription = db.session.query(Subscription).filter_by(
            stripe_subscription_id=stripe_subscription['id']
        ).first()
        
        if subscription:
            subscription.status = 'canceled'
            subscription.updated_at = datetime.utcnow()
            db.session.commit()
    except Exception as e:
        print(f"Error deleting subscription from webhook: {e}")

