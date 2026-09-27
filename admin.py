"""Admin dashboard: lets an admin set the global app price.

Setting the price to 0 makes the app free for all users - every account gets
unlimited generations regardless of its subscription plan.
"""
from functools import wraps

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, make_response
from flask_login import login_required, current_user

from models import db, User, PLANS, get_app_price, set_app_price, app_is_free

admin_bp = Blueprint('admin', __name__)


def admin_required(view):
    """Allow only logged-in admins (see ADMIN_USERNAMES) through."""
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not getattr(current_user, 'is_admin', False):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@admin_bp.route('/')
@admin_required
def dashboard():
    """Admin dashboard - shows the current app price and the controls to change it."""
    response = make_response(render_template(
        'admin.html',
        user=current_user,
        app_price=get_app_price(),
        is_free=app_is_free(),
        total_users=db.session.query(User).count(),
        all_plans=PLANS,
    ))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@admin_bp.route('/price', methods=['POST'])
@admin_required
def update_price():
    """Set the app price. A price of 0 makes the app free for all users."""
    try:
        price = set_app_price(request.form.get('price', ''))
    except ValueError as exc:
        flash(f'Could not update price: {exc}', 'error')
        return redirect(url_for('admin.dashboard'))

    if price == 0:
        flash('App price set to $0.00 - the app is now free for all users, with unlimited generations.', 'success')
    else:
        flash(f'App price set to ${price:.2f} - plan limits are back in effect.', 'success')
    return redirect(url_for('admin.dashboard'))


@admin_bp.route('/make-free', methods=['POST'])
@admin_required
def make_free():
    """Shortcut button: set the price to 0 so everyone uses the app for free."""
    set_app_price(0)
    flash('App price set to $0.00 - the app is now free for all users, with unlimited generations.', 'success')
    return redirect(url_for('admin.dashboard'))
