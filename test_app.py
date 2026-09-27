"""
Test suite for the Composition app.
Covers: auth, plan limits, unlimited plan (Eric), article generation, download endpoints.
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

os.environ['SECRET_KEY'] = 'test-secret-key'
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['OPENAI_API_KEY'] = 'sk-test-fake-key'

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app as flask_app
from models import (db, User, Subscription, QueryLog, PLANS,
                    get_app_price, set_app_price, app_is_free)
from werkzeug.security import generate_password_hash


@pytest.fixture
def app():
    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = False
    flask_app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def make_user(username='alice', email=None, password='pass123', plan=None, plan_end_days=30):
    """Helper: create user with optional subscription."""
    user = User(
        username=username,
        email=email or f'{username}@test.com',
        password_hash=generate_password_hash(password),
        is_active=True
    )
    db.session.add(user)
    db.session.flush()
    if plan and plan != 'free':
        now = datetime.utcnow()
        sub = Subscription(
            user_id=user.id,
            plan_type=plan,
            status='active',
            amount=PLANS[plan]['price'],
            currency='USD',
            current_period_start=now,
            current_period_end=now + timedelta(days=plan_end_days) if plan_end_days else None
        )
        db.session.add(sub)
    db.session.commit()
    return user


def set_price(price):
    """Helper: set the global app price (0 = free for everyone)."""
    return set_app_price(price)


def login(client, username, password='pass123'):
    return client.post('/auth/login', json={'username': username, 'password': password})


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------

class TestAuth:
    def test_register_new_user(self, client, app):
        r = client.post('/auth/register', json={
            'username': 'newuser', 'email': 'new@test.com', 'password': 'pass123'
        })
        assert r.status_code == 200
        data = r.get_json()
        assert data['success'] is True
        with app.app_context():
            assert User.query.filter_by(username='newuser').first() is not None

    def test_register_duplicate_username(self, client, app):
        with app.app_context():
            make_user('dup')
        r = client.post('/auth/register', json={
            'username': 'dup', 'email': 'other@test.com', 'password': 'pass123'
        })
        assert r.status_code == 400
        assert 'already exists' in r.get_json()['error']

    def test_register_short_password(self, client):
        r = client.post('/auth/register', json={
            'username': 'u', 'email': 'u@test.com', 'password': '12'
        })
        assert r.status_code == 400

    def test_login_success(self, client, app):
        with app.app_context():
            make_user('bob')
        r = login(client, 'bob')
        assert r.status_code == 200
        assert r.get_json()['success'] is True

    def test_login_wrong_password(self, client, app):
        with app.app_context():
            make_user('carol')
        r = client.post('/auth/login', json={'username': 'carol', 'password': 'wrong'})
        assert r.status_code == 401

    def test_login_by_email(self, client, app):
        with app.app_context():
            make_user('dave', email='dave@test.com')
        r = client.post('/auth/login', json={'username': 'dave@test.com', 'password': 'pass123'})
        assert r.status_code == 200
        assert r.get_json()['success'] is True

    def test_logout_redirects(self, client, app):
        with app.app_context():
            make_user('eve')
        login(client, 'eve')
        r = client.get('/auth/logout')
        assert r.status_code in (302, 200)

    def test_index_requires_login(self, client):
        r = client.get('/', follow_redirects=False)
        assert r.status_code == 302
        assert '/auth/login' in r.headers['Location']


# ---------------------------------------------------------------------------
# Plan / usage limit tests
# ---------------------------------------------------------------------------

class TestPlanLimits:
    def test_free_plan_has_3_monthly_queries(self, app):
        with app.app_context():
            user = make_user('free1', plan=None)
            assert user.get_plan() == 'free'
            assert PLANS['free']['queries_per_month'] == 3

    def test_free_user_can_make_query_initially(self, app):
        with app.app_context():
            user = make_user('free2', plan=None)
            assert user.can_make_query() is True

    def test_free_user_blocked_after_limit(self, app):
        with app.app_context():
            set_price(9.99)
            user = make_user('free3', plan=None)
            # Exhaust free quota
            for _ in range(3):
                db.session.add(QueryLog(user_id=user.id, topic='test', language='English'))
            db.session.commit()
            assert user.can_make_query() is False

    def test_unlimited_plan_always_allows_query(self, app):
        with app.app_context():
            user = make_user('unlim', plan='unlimited', plan_end_days=None)
            # Add many queries
            for _ in range(1000):
                db.session.add(QueryLog(user_id=user.id, topic='t', language='English'))
            db.session.commit()
            assert user.can_make_query() is True

    def test_unlimited_get_remaining_returns_minus_one(self, app):
        with app.app_context():
            user = make_user('unlim2', plan='unlimited', plan_end_days=None)
            assert user.get_remaining_queries() == -1

    def test_elite_plan_daily_limit(self, app):
        with app.app_context():
            set_price(9.99)
            user = make_user('elite1', plan='elite')
            assert PLANS['elite']['queries_per_day'] == 10
            # Use 10 queries today
            for _ in range(10):
                db.session.add(QueryLog(user_id=user.id, topic='t', language='English'))
            db.session.commit()
            assert user.can_make_query() is False

    def test_basic_plan_weekly_limit(self, app):
        with app.app_context():
            set_price(9.99)
            user = make_user('basic1', plan='basic')
            assert PLANS['basic']['queries_per_week'] == 3
            for _ in range(3):
                db.session.add(QueryLog(user_id=user.id, topic='t', language='English'))
            db.session.commit()
            assert user.can_make_query() is False

    def test_advanced_plan_monthly_limit(self, app):
        with app.app_context():
            set_price(9.99)
            user = make_user('adv1', plan='advanced')
            assert PLANS['advanced']['queries_per_month'] == 100
            for _ in range(100):
                db.session.add(QueryLog(user_id=user.id, topic='t', language='English'))
            db.session.commit()
            assert user.can_make_query() is False

    def test_expired_subscription_falls_back_to_free(self, app):
        with app.app_context():
            user = make_user('exp1')
            past = datetime.utcnow() - timedelta(days=5)
            sub = Subscription(
                user_id=user.id,
                plan_type='elite',
                status='active',
                amount=9.99,
                currency='USD',
                current_period_start=past - timedelta(days=30),
                current_period_end=past
            )
            db.session.add(sub)
            db.session.commit()
            assert user.get_plan() == 'free'


# ---------------------------------------------------------------------------
# Eric unlimited account
# ---------------------------------------------------------------------------

class TestEricUnlimited:
    def test_eric_exists(self, app):
        """Eric was created by set_unlimited_user.py — not in the in-memory DB.
        This test verifies the unlimited plan logic works for a user named Eric."""
        with app.app_context():
            eric = make_user('Eric', email='eric@test.com', plan='unlimited', plan_end_days=None)
            assert eric.get_plan() == 'unlimited'
            assert eric.can_make_query() is True
            assert eric.get_remaining_queries() == -1

    def test_eric_unlimited_subscription_has_no_expiry(self, app):
        with app.app_context():
            eric = make_user('Eric2', email='eric2@test.com', plan='unlimited', plan_end_days=None)
            sub = eric.get_active_subscription()
            assert sub is not None
            assert sub.plan_type == 'unlimited'
            assert sub.current_period_end is None


# ---------------------------------------------------------------------------
# Generate endpoint
# ---------------------------------------------------------------------------

class TestGenerateEndpoint:
    def _login_user(self, client, app, username='gen1', plan=None):
        with app.app_context():
            make_user(username, plan=plan)
        login(client, username)

    def test_generate_requires_login(self, client):
        r = client.post('/generate', json={'topic': 'cats', 'language': 'English'})
        assert r.status_code in (302, 401)

    @patch('app.client')
    def test_generate_success(self, mock_openai, client, app):
        mock_choice = MagicMock()
        mock_choice.message.content = 'This is a test article about cats.'
        mock_openai.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

        with app.app_context():
            make_user('gen2')
        login(client, 'gen2')
        r = client.post('/generate', json={
            'topic': 'cats', 'language': 'English',
            'total_words': 100, 'grade': 1, 'language_level': 'intermediate'
        })
        assert r.status_code == 200
        data = r.get_json()
        assert data['success'] is True
        assert 'article' in data

    @patch('app.client')
    def test_generate_blocked_when_limit_reached(self, mock_openai, client, app):
        with app.app_context():
            set_price(9.99)
            user = make_user('gen3', plan=None)
            for _ in range(3):
                db.session.add(QueryLog(user_id=user.id, topic='t', language='English'))
            db.session.commit()
        login(client, 'gen3')
        r = client.post('/generate', json={
            'topic': 'dogs', 'language': 'English',
            'total_words': 100, 'grade': 1, 'language_level': 'intermediate'
        })
        assert r.status_code == 403
        assert 'limit' in r.get_json()['error'].lower()

    @patch('app.client')
    def test_generate_missing_topic(self, mock_openai, client, app):
        with app.app_context():
            make_user('gen4')
        login(client, 'gen4')
        r = client.post('/generate', json={'language': 'English', 'total_words': 100})
        assert r.status_code == 400

    @patch('app.client')
    def test_generate_logs_query(self, mock_openai, client, app):
        mock_choice = MagicMock()
        mock_choice.message.content = 'Article content here.'
        mock_openai.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

        with app.app_context():
            user = make_user('gen5')
            uid = user.id
        login(client, 'gen5')
        client.post('/generate', json={
            'topic': 'fish', 'language': 'English',
            'total_words': 50, 'grade': 1, 'language_level': 'beginner'
        })
        with app.app_context():
            count = QueryLog.query.filter_by(user_id=uid).count()
            assert count == 1


# ---------------------------------------------------------------------------
# Download endpoint
# ---------------------------------------------------------------------------

class TestDownloadEndpoint:
    SAMPLE_ARTICLE = 'Hello world. This is a test article.'

    def _setup_and_login(self, client, app, username):
        with app.app_context():
            make_user(username)
        login(client, username)

    def test_download_txt(self, client, app):
        self._setup_and_login(client, app, 'dl1')
        r = client.post('/download', json={
            'article': self.SAMPLE_ARTICLE, 'format': 'txt', 'filename': 'test'
        })
        assert r.status_code == 200
        assert r.mimetype == 'text/plain'

    def test_download_doc(self, client, app):
        self._setup_and_login(client, app, 'dl2')
        r = client.post('/download', json={
            'article': self.SAMPLE_ARTICLE, 'format': 'doc', 'filename': 'test'
        })
        assert r.status_code == 200
        assert 'wordprocessingml' in r.mimetype

    def test_download_pdf(self, client, app):
        self._setup_and_login(client, app, 'dl3')
        r = client.post('/download', json={
            'article': self.SAMPLE_ARTICLE, 'format': 'pdf', 'filename': 'test'
        })
        assert r.status_code == 200
        assert r.mimetype == 'application/pdf'

    def test_download_html(self, client, app):
        self._setup_and_login(client, app, 'dl4')
        r = client.post('/download', json={
            'article': self.SAMPLE_ARTICLE, 'format': 'html', 'filename': 'test'
        })
        assert r.status_code == 200
        assert 'html' in r.mimetype

    def test_download_invalid_format(self, client, app):
        self._setup_and_login(client, app, 'dl5')
        r = client.post('/download', json={
            'article': self.SAMPLE_ARTICLE, 'format': 'xyz', 'filename': 'test'
        })
        assert r.status_code == 400

    def test_download_empty_article(self, client, app):
        self._setup_and_login(client, app, 'dl6')
        r = client.post('/download', json={'article': '', 'format': 'txt', 'filename': 'test'})
        assert r.status_code == 400

    def test_download_requires_login(self, client):
        r = client.post('/download', json={
            'article': self.SAMPLE_ARTICLE, 'format': 'txt', 'filename': 'test'
        })
        assert r.status_code in (302, 401)


# ---------------------------------------------------------------------------
# App price / free-for-all-users
# ---------------------------------------------------------------------------

class TestAppPrice:
    def test_default_price_is_zero_and_app_is_free(self, app):
        with app.app_context():
            assert get_app_price() == 0
            assert app_is_free() is True

    def test_set_and_get_price_roundtrip(self, app):
        with app.app_context():
            set_price(12.5)
            assert get_app_price() == 12.5
            assert app_is_free() is False

    def test_price_zero_makes_app_free(self, app):
        with app.app_context():
            set_price(9.99)
            assert app_is_free() is False
            set_price(0)
            assert app_is_free() is True

    def test_negative_price_rejected(self, app):
        with app.app_context():
            with pytest.raises(ValueError):
                set_app_price(-1)

    def test_non_numeric_price_rejected(self, app):
        with app.app_context():
            with pytest.raises(ValueError):
                set_app_price('free')

    def test_free_app_lifts_limits_for_exhausted_free_user(self, app):
        with app.app_context():
            set_price(9.99)
            user = make_user('pf1', plan=None)
            for _ in range(10):
                db.session.add(QueryLog(user_id=user.id, topic='t', language='English'))
            db.session.commit()
            assert user.can_make_query() is False

            set_price(0)
            assert user.can_make_query() is True
            assert user.get_remaining_queries() == -1

    @patch('app.client')
    def test_generate_allowed_for_all_when_free(self, mock_openai, client, app):
        mock_choice = MagicMock()
        mock_choice.message.content = 'Free article.'
        mock_openai.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

        with app.app_context():
            set_price(0)
            user = make_user('pf2', plan=None)
            # Way past the free plan's monthly allowance
            for _ in range(20):
                db.session.add(QueryLog(user_id=user.id, topic='t', language='English'))
            db.session.commit()
        login(client, 'pf2')
        r = client.post('/generate', json={
            'topic': 'birds', 'language': 'English',
            'total_words': 100, 'grade': 1, 'language_level': 'intermediate'
        })
        assert r.status_code == 200
        assert r.get_json()['success'] is True

    def test_checkout_blocked_while_free(self, client, app):
        with app.app_context():
            set_price(0)
            make_user('pf3', plan=None)
        login(client, 'pf3')
        r = client.post('/payment/create-checkout-session', json={'plan_type': 'elite'})
        assert r.status_code == 400
        assert 'free' in r.get_json()['error'].lower()


# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------

class TestAdminDashboard:
    def test_admin_requires_login(self, client):
        r = client.get('/admin/', follow_redirects=False)
        assert r.status_code in (302, 401)

    def test_non_admin_forbidden(self, client, app):
        with app.app_context():
            make_user('notadmin', plan=None)
        login(client, 'notadmin')
        r = client.get('/admin/')
        assert r.status_code == 403

    def test_admin_user_flag(self, app):
        with app.app_context():
            eric = make_user('Eric', email='eric@test.com', plan='unlimited', plan_end_days=None)
            other = make_user('someone_else', plan=None)
            assert eric.is_admin is True
            assert other.is_admin is False

    def test_admin_can_open_dashboard(self, client, app):
        with app.app_context():
            make_user('Eric', email='eric@test.com', plan='unlimited', plan_end_days=None)
        login(client, 'Eric')
        r = client.get('/admin/')
        assert r.status_code == 200
        assert b'Adjust app price' in r.data

    def test_admin_can_set_price(self, client, app):
        with app.app_context():
            make_user('Eric', email='eric@test.com', plan='unlimited', plan_end_days=None)
        login(client, 'Eric')
        r = client.post('/admin/price', data={'price': '7.50'}, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert get_app_price() == 7.5
            assert app_is_free() is False

    def test_admin_can_make_app_free(self, client, app):
        with app.app_context():
            make_user('Eric', email='eric@test.com', plan='unlimited', plan_end_days=None)
            set_price(19.99)
        login(client, 'Eric')
        r = client.post('/admin/make-free', follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert get_app_price() == 0
            assert app_is_free() is True

    def test_admin_price_zero_via_form_makes_free(self, client, app):
        with app.app_context():
            make_user('Eric', email='eric@test.com', plan='unlimited', plan_end_days=None)
            set_price(4.99)
        login(client, 'Eric')
        client.post('/admin/price', data={'price': '0'}, follow_redirects=False)
        with app.app_context():
            assert app_is_free() is True

    def test_admin_rejects_invalid_price(self, client, app):
        with app.app_context():
            make_user('Eric', email='eric@test.com', plan='unlimited', plan_end_days=None)
            set_price(4.99)
        login(client, 'Eric')
        r = client.post('/admin/price', data={'price': 'abc'}, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert get_app_price() == 4.99

    def test_non_admin_cannot_set_price(self, client, app):
        with app.app_context():
            make_user('plainuser', plan=None)
            set_price(4.99)
        login(client, 'plainuser')
        r = client.post('/admin/price', data={'price': '0'})
        assert r.status_code == 403
        with app.app_context():
            assert get_app_price() == 4.99
