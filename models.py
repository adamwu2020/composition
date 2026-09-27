import os
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime, timedelta

db = SQLAlchemy()

# Usernames allowed into the admin dashboard (comma-separated).
ADMIN_USERNAMES = [
    name.strip() for name in os.getenv('ADMIN_USERNAMES', 'Eric').split(',') if name.strip()
]

# App price when the admin has never set one.
DEFAULT_APP_PRICE = float(os.getenv('DEFAULT_APP_PRICE', '0'))

APP_PRICE_KEY = 'app_price'

# Plan definitions
PLANS = {
    'free': {
        'name': 'Free',
        'price': 0,
        'interval': 'month',
        'queries_per_month': 3,
        'queries_per_week': None,
        'queries_per_day': None
    },
    'basic': {
        'name': 'Basic',
        'price': 4.99,
        'interval': 'month',
        'queries_per_month': None,
        'queries_per_week': 3,
        'queries_per_day': None
    },
    'elite': {
        'name': 'Elite',
        'price': 9.99,
        'interval': 'month',
        'queries_per_month': None,
        'queries_per_week': None,
        'queries_per_day': 10
    },
    'advanced': {
        'name': 'Advanced',
        'price': 19.99,
        'interval': 'month',
        'queries_per_month': 100,
        'queries_per_week': None,
        'queries_per_day': None
    },
    'annual': {
        'name': 'Annual Pass',
        'price': 199.99,
        'interval': 'year',
        'queries_per_month': 100,
        'queries_per_week': None,
        'queries_per_day': None
    },
    'unlimited': {
        'name': 'Unlimited',
        'price': 0,
        'interval': 'month',
        'queries_per_month': None,
        'queries_per_week': None,
        'queries_per_day': None
    }
}

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    
    # Relationship to subscriptions
    subscriptions = db.relationship('Subscription', backref='user', lazy=True, cascade='all, delete-orphan')
    # Relationship to query logs
    query_logs = db.relationship('QueryLog', backref='user', lazy=True, cascade='all, delete-orphan')
    
    @property
    def is_admin(self):
        """Admins are named in the ADMIN_USERNAMES env var (default: Eric)."""
        return self.username in ADMIN_USERNAMES

    def get_active_subscription(self):
        """Get the active subscription if any"""
        # Force fresh query - expire all caches
        from sqlalchemy.orm import object_session
        session = object_session(self)
        if session:
            session.expire_all()
        
        # First try to get an active subscription (status='active' and not expired)
        now = datetime.utcnow()
        
        # Query for active subscriptions, ordered by most recent first
        # CRITICAL: Only get subscriptions with status='active' (exclude 'canceled')
        # Use a fresh query to bypass any cache
        # IMPORTANT: Order by updated_at DESC first, then created_at DESC to ensure most recent is first
        # This is critical for downgrades where new subscription should be returned even if timestamps are close
        subscription = db.session.query(Subscription).filter(
            Subscription.user_id == self.id,
            Subscription.status == 'active'  # CRITICAL: Only active subscriptions
        ).filter(
            (Subscription.current_period_end.is_(None)) | 
            (Subscription.current_period_end > now)
        ).order_by(
            Subscription.updated_at.desc(),  # Most recently updated first
            Subscription.created_at.desc(),  # Then most recently created
            Subscription.id.desc()  # Finally by ID as tiebreaker
        ).first()
        
        # Debug: Log what we found
        if subscription:
            print(f"get_active_subscription() found: ID={subscription.id}, Plan={subscription.plan_type}, Status={subscription.status}, Updated={subscription.updated_at}")
        else:
            # If no active subscription, check if there are any subscriptions at all
            all_subs = db.session.query(Subscription).filter_by(user_id=self.id).all()
            print(f"get_active_subscription() - No active subscription found. Total subscriptions: {len(all_subs)}")
            for sub in all_subs:
                print(f"  - ID: {sub.id}, Plan: {sub.plan_type}, Status: {sub.status}")
            # Return None - user will default to free plan
            subscription = None
        
        # If no subscription at all, return None (user will default to free plan)
        return subscription
    
    def has_active_subscription(self):
        """Check if user has an active subscription"""
        subscription = self.get_active_subscription()
        if subscription:
            return subscription.is_active()
        # Free plan users don't need active subscription
        return True
    
    def get_plan(self):
        """Get the user's current plan"""
        # Force complete cache expiration
        from sqlalchemy.orm import object_session
        session = object_session(self)
        if session:
            # Expire all cached relationships and attributes
            session.expire(self)
            session.expire_all()
        
        # Get fresh subscription from database
        subscription = self.get_active_subscription()
        
        # Double-check: if subscription exists, verify it's actually active
        if subscription:
            # Refresh the subscription object to ensure we have latest data
            if session:
                session.refresh(subscription)
            
            # Check if it's actually active
            if subscription.is_active():
                return subscription.plan_type
            else:
                print(f"Subscription {subscription.id} exists but is not active: status={subscription.status}")
        
        return 'free'
    
    def can_make_query(self):
        """Check if user can make a query based on their plan limits"""
        # When the admin has set the app price to 0, the app is free for everyone
        # and no plan limit applies.
        if app_is_free():
            return True

        plan_type = self.get_plan()
        plan = PLANS.get(plan_type, PLANS['free'])
        
        # Get query counts for different periods
        now = datetime.utcnow()
        
        if plan['queries_per_day']:
            # Check daily limit
            today_start = datetime(now.year, now.month, now.day)
            today_queries = QueryLog.query.filter_by(
                user_id=self.id
            ).filter(QueryLog.created_at >= today_start).count()
            return today_queries < plan['queries_per_day']
        
        elif plan['queries_per_week']:
            # Check weekly limit
            week_start = now - timedelta(days=now.weekday())
            week_start = datetime(week_start.year, week_start.month, week_start.day)
            week_queries = QueryLog.query.filter_by(
                user_id=self.id
            ).filter(QueryLog.created_at >= week_start).count()
            return week_queries < plan['queries_per_week']
        
        elif plan['queries_per_month']:
            # Check monthly limit
            month_start = datetime(now.year, now.month, 1)
            month_queries = QueryLog.query.filter_by(
                user_id=self.id
            ).filter(QueryLog.created_at >= month_start).count()
            return month_queries < plan['queries_per_month']
        
        return True
    
    def get_remaining_queries(self):
        """Get remaining queries for current period (-1 means unlimited)"""
        if app_is_free():
            return -1

        plan_type = self.get_plan()
        plan = PLANS.get(plan_type, PLANS['free'])
        
        now = datetime.utcnow()
        
        if plan['queries_per_day']:
            today_start = datetime(now.year, now.month, now.day)
            used = QueryLog.query.filter_by(
                user_id=self.id
            ).filter(QueryLog.created_at >= today_start).count()
            return max(0, plan['queries_per_day'] - used)
        
        elif plan['queries_per_week']:
            week_start = now - timedelta(days=now.weekday())
            week_start = datetime(week_start.year, week_start.month, week_start.day)
            used = QueryLog.query.filter_by(
                user_id=self.id
            ).filter(QueryLog.created_at >= week_start).count()
            return max(0, plan['queries_per_week'] - used)
        
        elif plan['queries_per_month']:
            month_start = datetime(now.year, now.month, 1)
            used = QueryLog.query.filter_by(
                user_id=self.id
            ).filter(QueryLog.created_at >= month_start).count()
            return max(0, plan['queries_per_month'] - used)

        return -1  # Unlimited

class Subscription(db.Model):
    __tablename__ = 'subscriptions'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    stripe_subscription_id = db.Column(db.String(255), unique=True, nullable=True)
    stripe_customer_id = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(50), default='inactive')  # active, canceled, past_due, etc.
    plan_type = db.Column(db.String(50), default='free')  # free, basic, elite, advanced, annual
    amount = db.Column(db.Numeric(10, 2), default=0)
    currency = db.Column(db.String(3), default='USD')
    current_period_start = db.Column(db.DateTime, nullable=True)
    current_period_end = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def is_active(self):
        """Check if subscription is currently active"""
        if self.plan_type == 'free':
            return True  # Free plan is always "active"
        if self.status == 'active':
            if self.current_period_end:
                return datetime.utcnow() < self.current_period_end
            return True
        return False
    
    def days_remaining(self):
        """Get days remaining in subscription"""
        if self.current_period_end and self.is_active():
            remaining = self.current_period_end - datetime.utcnow()
            return max(0, remaining.days)
        return 0

class QueryLog(db.Model):
    __tablename__ = 'query_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    topic = db.Column(db.String(255), nullable=True)
    language = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AppSetting(db.Model):
    """Global, admin-editable app settings stored as key/value pairs."""
    __tablename__ = 'app_settings'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def get_setting(key, default=None):
    """Read a setting, falling back to default if unset or the table is missing."""
    try:
        setting = db.session.query(AppSetting).filter_by(key=key).first()
    except Exception:
        # Table may not exist yet (first boot before create_all).
        db.session.rollback()
        return default
    if setting is None or setting.value is None:
        return default
    return setting.value


def set_setting(key, value):
    """Write a setting, creating the row if needed."""
    setting = db.session.query(AppSetting).filter_by(key=key).first()
    if setting is None:
        setting = AppSetting(key=key, value=str(value))
        db.session.add(setting)
    else:
        setting.value = str(value)
    db.session.commit()
    return setting


def get_app_price():
    """Current app price in dollars. 0 means the app is free for all users."""
    raw = get_setting(APP_PRICE_KEY)
    if raw is None:
        return DEFAULT_APP_PRICE
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_APP_PRICE


def set_app_price(price):
    """Set the app price in dollars. Raises ValueError on invalid input."""
    try:
        value = float(price)
    except (TypeError, ValueError):
        raise ValueError('Price must be a number')
    if value < 0:
        raise ValueError('Price cannot be negative')
    if value > 100000:
        raise ValueError('Price is unreasonably high')
    value = round(value, 2)
    set_setting(APP_PRICE_KEY, f'{value:.2f}')
    return value


def app_is_free():
    """True when the admin has set the app price to 0 - everyone gets unlimited use."""
    return get_app_price() <= 0
