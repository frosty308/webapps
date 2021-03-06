#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
Copyright (c) 2017-2021 Alan Frost. All rights reserved.

Implementation of Web server using Flask framework

"""

from __future__ import print_function

import logging
import signal
import socket
from datetime import datetime
import time
from urlparse import urlparse, urljoin
import json
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect
import jinja2

from botocore.exceptions import EndpointConnectionError, ClientError
from flask import (Flask, make_response, request, render_template, redirect, jsonify,
                   abort, flash, url_for)
from flask_login import (LoginManager, current_user, login_required, login_user, logout_user,
                         fresh_login_required)
import pytz
from decorators import async
from forms import (AcceptForm, ChangePasswordForm, ConfirmForm, ForgotPasswordForm,
                   InviteForm, LoginForm, RegistrationForm, VerifyForm, ResetPasswordForm,
                   ResendForm, UploadForm)
from crypto import derive_key
from utils import (load_config, generate_timed_token, validate_timed_token, generate_user_id,
                   generate_random58_id, generate_random_int, preset_password,
                   generate_otp_secret, generate_hotp_code, verify_hotp_code, get_ip_address,
                   check_code, check_phone, sanitize_name, get_user_agent)
from awsutils import DynamoDB, SNS, SES, S3
from recipe import RecipeManager
from vault import VaultManager
from events import EventManager

CONFIG = load_config('config.json')

USERS = DynamoDB(CONFIG, CONFIG.get('users'))
SESSIONS = DynamoDB(CONFIG, CONFIG.get('sessions'))
RECIPE_MANAGER = RecipeManager(CONFIG)
RECIPE_MANAGER.load_recipes('recipes.json')
RECIPE_MANAGER.load_references('sauces.json')
RECIPE_LIST = RECIPE_MANAGER.build_search_list()
VAULT_MANAGER = VaultManager(CONFIG)
EVENT_MANAGER = EventManager(CONFIG)
#SNS = SNS('FrostyWeb')
#SES = SES('Alan Frost <alan@cyberfrosty.com>')

# Log exceptions and errors to /var/log/cyberfrosty.log
# 2017-05-11 08:29:26,696 ERROR webapp:main [Errno 51] Network is unreachable
LOGGER = logging.getLogger("CyberFrosty")


SERVER_VERSION = '0.1'
SERVER_START = int((datetime.now(tz=pytz.utc) -
                    datetime(1970, 1, 1, tzinfo=pytz.utc)).total_seconds())
MAX_FAILURES = 3
LOCK_TIME = 1800
LOGIN_MANAGER = LoginManager()
APP = Flask(__name__, static_url_path="")

APP.config['SECRET_KEY'] = 'super secret key'
APP.config['SSL_DISABLE'] = False
APP.config['SESSION_COOKIE_HTTPONLY'] = True
APP.config['REMEMBER_COOKIE_HTTPONLY'] = True
APP.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 # Limit uploads to 16MB

LOGIN_MANAGER.init_app(APP)
LOGIN_MANAGER.login_view = "login"
LOGIN_MANAGER.login_message = "Please login to access this page"
LOGIN_MANAGER.session_protection = "strong"
CSRF = CSRFProtect(APP)

@async
def send_email(recipient, subject, action, **kwargs):
    """ Send an email from a new thread
    Args:
        recipient
        email subject line
        action template
        arguments for templating
    """
    env = jinja2.Environment(loader=jinja2.FileSystemLoader('./templates'))
    template = env.get_template('email/' + action + '.txt')
    text = template.render(**kwargs)
    template = env.get_template('email/' + action + '.html')
    html = template.render(title=subject, **kwargs)

    #with APP.app_context():
    #    try:
    #        SES.send_email(recipient, subject, html, text)
    #    except ClientError as err:
    #        print(err.response['Error']['Message'])
    print(subject)

@async
def send_text(phone, msg):
    """ Send a text message from a new thread
    Args:
        number: phone number (e.g. '+17702233322')
        message: text
    """
    #with APP.app_context():
    #    try:
    #        SNS.send_sms(phone, msg)
    #    except ClientError as err:
    #        print(err.response['Error']['Message'])
    print(msg)

class User(object):
    """ Class for the current user
    """
    def __init__(self, email, user=None):
        """ Constructor
        """
        self._email = email
        self._userid = generate_user_id(CONFIG.get('user_id_hmac'), email)
        self._user = user
        self._authenticated = False
        self._active = False

    @property
    def is_authenticated(self):
        return self._authenticated

    @is_authenticated.setter
    def is_authenticated(self, value):
        self._authenticated = value

    @property
    def is_active(self):
        return self._active

    @is_active.setter
    def is_active(self, value):
        self._active = value

    def generate_token(self, action):
        """ Generate a timed token, tied to user name and action
        Args:
            action: confirm, delete, register, reset, etc.
        Return:
            URL safe encoded token
        """
        return generate_timed_token(self._email, APP.config['SECRET_KEY'], action)

    def validate_token(self, token, action):
        """ Validate a timed token, tied to user name and action
        Args:
            token
            action: confirm, delete, register, reset, etc.
        Return:
            True or False
        """
        validated, value = validate_timed_token(token, APP.config['SECRET_KEY'], action)
        if validated and value == self._email:
            return True
        return False

    @property
    def is_anonymous(self):
        return False

    def get_id(self):
        return self._userid

    def get_email(self):
        return self._email

    def get_user(self):
        return self._user

@LOGIN_MANAGER.user_loader
def load_user(userid):
    """ Load user account details from database
    Args:
        userid
    """
    session = SESSIONS.get_item('id', userid)
    if 'error' in session:
        account = USERS.get_item('id', userid)
        if 'error' not in account:
            name = account.get('user')
            print('Loaded user: {}'.format(account.get('email')))
            user = User(account.get('email'), name)
        else:
            print('Anonymous user')
            user = User('anonymous@unknown.com', 'Anonymous')
            user.is_authenticated = False
            user.is_active = False
    else:
        name = session.get('user')
        print('Loaded session: {}'.format(session.get('email')))
        user = User(session.get('email'), name)
        user.is_authenticated = session.get('failures', 0) < MAX_FAILURES
        user.is_active = True
    return user

@LOGIN_MANAGER.unauthorized_handler
def unauthorized_page():
    """ Called when @login_required decorator triggers, redirects to login page and after
        success redirects back to referring page
    """
    return redirect(url_for('login', next=request.path))

def user_authenticated(userid, account, session, agent, action, remember=False):
    """ User has authenticated, reflect that in session and call login_user
    """
    EVENT_MANAGER.web_event(action, userid, **agent)
    agent['at'] = datetime.today().ctime()
    if 'error' in session: # An error means no session entry exists
        del session['error']
        session['id'] = userid
        session['email'] = account.get('email')
        session['user'] = account.get('user')
        session['logins'] = agent
        session['failures'] = 0
        SESSIONS.put_item(session)
    else:
        # Reset failed login counter if needed and clear locked
        if session['failures'] != 0:
            session['failures'] = 0
            if 'locked_at' in session:
                del session['locked_at']
            session['logins'] = agent
            SESSIONS.put_item(session)
        else:
            SESSIONS.update_item('id', userid, 'logins', agent)
    user = User(account.get('email'), account.get('user'))
    user.is_authenticated = True
    user.is_active = True
    login_user(user, remember=remember)

def get_parameter(response, param, default=None):
    """ Get named parameter from url, json or either of two types of form encoding
    Args:
        response: dictionary of HTTP response
        param: key to look for
    Returns:
        value or parameter or None
    """
    value = response.args.get(param)
    if not value and response.json:
        value = response.json.get(param)
    if not value:
        content_type = response.headers.get('Content-Type')
        if content_type:
            if content_type == 'application/x-www-form-urlencoded' or \
               content_type.startswith('multipart/form-data'):
                value = response.form.get(param)
    if not value:
        return default
    else:
        return value

def is_safe_url(target):
    """ Ensure that the redirect URL refers to the same host and not to an attackers site.
    Args:
        target url
    Returns:
        True if target url is safe
    """
    # Check for open redirect vulnerability, which allows ///host.com to be parsed as a path
    if '///' in target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and \
           ref_url.netloc == test_url.netloc

def get_redirect_target():
    """ Look for the 'next' parameter or use the request object to find the redirect target.
        This function is used for login and other actions where after success the user is
        redirected back to the page, from which they were redirected to login first.
    Returns:
        redirect URL or None
    """
    for target in get_parameter(request, 'next'), request.referrer:
        if target and is_safe_url(target):
            return target

def redirect_back(endpoint, **values):
    """ Redirect back to next url, if missing or not safe defaults to endpoint url
    Args:
        endpoint url
        value parameters
    """
    target = get_parameter(request, 'next')
    if not target or not is_safe_url(target):
        target = url_for(endpoint, **values)
    return redirect(target)

def allowed_file(filename):
    """ Only allow specific file types to be uploaded
    Args:
        filename
    Returns:
        True if file type is allowed
    """
    extensions = set(['csv', 'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif'])
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in extensions

def failed_account_attempt(session, failures, failmsg=None):
    """ Handle a failed attempt (login, accept, confirm, change...)
    Args:
        session object
        failure count
        optional fail message
    Returns:
        True if file type is allowed
    """
    failures += 1
    if 'error' in session: # An error means no session entry exists
        del session['error']
        session['failures'] = 1
        SESSIONS.put_item(session)
        errmsg = failmsg or 'Unable to validate your credentials'
    elif failures > MAX_FAILURES:
        session['locked_at'] = int(time.mktime(datetime.utcnow().timetuple()))
        session['failures'] = failures
        SESSIONS.put_item(session)
        errmsg = 'Your account has been locked'
    else:
        SESSIONS.update_item('id', session.get('id'), 'failures', failures)
        errmsg = failmsg or 'Unable to validate your credentials'
    return errmsg

def check_account_lock(session):
    """ Check for an account lock and return the appropriate number of failures
    Args:
        session object
    Returns:
        correct failure count
    """
    failures = session.get('failures', 0)
    if failures > MAX_FAILURES:
        if 'locked_at' in session:
            locktime = int(time.mktime(datetime.utcnow().timetuple())) - session['locked_at']
            if locktime > LOCK_TIME:
                failures = MAX_FAILURES  # Locked time expired, reset failure counter for one chance
        else:
            SESSIONS.update_item('id', session.get('id'),
                                 'locked_at', int(time.mktime(datetime.utcnow().timetuple())))
    return failures

def validate_credentials(form):
    """ Validate user credentials and update sessions database for timed token link page
        Accept Invitation, Reset Password
    Args:
        form object
    Returns:
        form with 'errors' set as appropriate
    """
    errmsg = None
    email = form.email.data
    token = form.token.data
    action = form.action.data
    code = form.code.data if 'code' in form else None
    agent = {"ip": get_ip_address(request), "from": get_user_agent(request)}
    userid = generate_user_id(CONFIG.get('user_id_hmac'), email) if email else 'Unknown'
    account = USERS.get_item('id', userid)
    if 'error' in account:
        errmsg = 'Unable to validate your credentials'
        EVENT_MANAGER.error_event(action, userid, 'Unregistered email', **agent)
        form.errors[action.capitalize()] = [errmsg]
        return form
    if action == 'reset' and 'reset_mcf' not in account:
        errmsg = 'No pending password reset request'
        EVENT_MANAGER.error_event(action, userid, errmsg, **agent)
        form.errors[action.capitalize()] = [errmsg]
        return form

    userid = account.get('id')
    session = SESSIONS.get_item('id', userid)
    if 'error' in session: # error means there is no existing session, so create one
        session['id'] = userid
        session['email'] = account.get('email')
        session['user'] = account.get('user')

    # Check for account locked
    failures = check_account_lock(session)
    session['failures'] = failures
    if failures > MAX_FAILURES:
        errmsg = 'Your account is locked'
    else:
        # Validate token, code, then password
        validated, value = validate_timed_token(token, APP.config['SECRET_KEY'], action)
        if validated and value == email:
            if account.get('authentication') == 'password:sms':
                errmsg = verify_code(account, code)
            # Check the temporary password now
            if errmsg is None:
                mcf = 'reset_mcf' if action == 'reset' else 'mcf'
                old_mcf = account.get(mcf)
                mcf = derive_key(form.oldpassword.data, old_mcf)
                if mcf != old_mcf:
                    print('old password failed')
                    errmsg = failed_account_attempt(session, failures)
                else:
                    mcf = derive_key(form.password.data)
                    response = USERS.update_item('id', userid, 'mcf', mcf)
        else:
            errmsg = 'The reset link is invalid or has expired'
    if errmsg:
        errmsg = failed_account_attempt(session, failures, errmsg)
        form.errors[action.capitalize()] = [errmsg]
        EVENT_MANAGER.error_event(action, userid, errmsg, **agent)
        form.password.data = ''
    else:
        if action == 'invite':
            user = form.user.data if 'user' in form else None
            phone = form.phone.data if 'phone' in form else None
            # Update user account status and name/phone if changed from invite
            if user != account.get('user'):
                response = USERS.update_item('id', userid, 'user', user)
                if 'error' in response:
                    print(response['error'])
            if check_phone(phone) and phone != account.get('phone'):
                response = USERS.update_item('id', userid, 'phone', phone)
            created = 'accepted: ' + datetime.utcnow().strftime('%Y-%m-%d')
            response = USERS.update_item('id', userid, 'created', created)
            if 'error' in response:
                print(response['error'])
            flash('You have confirmed your account. Thanks!')
        elif action == 'register' and account['created'][:7] == 'pending':
            created = 'registered: ' + datetime.utcnow().strftime('%Y-%m-%d')
            response = USERS.update_item('id', userid, 'created', created)
            if 'error' in response:
                print(response['error'])
            flash('You have confirmed your account. Thanks!')
        elif action == 'reset':
            flash('You have reset your password.')
        user_authenticated(userid, account, session, agent, 'reset')

    return form

def send_code(account, action):
    """ Send an authorization code to the user
    Args:
        account info
        action
    Returns:
        True if code sent
    """
    authentication = account.get('authentication')
    if authentication == 'password:authy':
        #send_authy_token_request(user.authy_id)
        print('Sent Authy code')
    elif authentication == 'password:sms' and 'phone' in account:
        secret, counter = account.get('otp').split(':')
        counter = int(counter) + 1
        code = generate_hotp_code(secret, counter)
        response = USERS.update_item('id', account['id'], 'otp', secret + ':' + str(counter))
        send_text(account.get('phone'), code + ' is your Frosty Web code')
    elif authentication == 'password' and 'phone' in account and action in ['enable', 'invite', 'register', 'reset']:
        if 'otp' not in account:
            secret = generate_otp_secret()
            counter = generate_random_int()
        else:
            secret, counter = account.get('otp').split(':')
            counter = int(counter) + 1
        code = generate_hotp_code(secret, counter)
        response = USERS.update_item('id', account['id'], 'otp', secret + ':' + str(counter))
        send_text(account.get('phone'), code + ' is your Frosty Web code')
    else:
        return None
    return True

def verify_code(account, code):
    """ Verify an authorization code from the user
    Args:
        account info
        code to verify
    Returns:
        errmsg for failure or None for success
    """
    errmsg = None
    if check_code(code):
        if 'otp' not in account:
            errmsg = 'Unable to validate your credentials'
        else:
            secret, counter = account.get('otp').split(':')
            counter = int(counter)
            verified = verify_hotp_code(secret, code, counter)
            if verified is None:
                print('verify_code failed({}, {}, {})'.format(secret, code, counter))
                errmsg = 'The code is invalid or has expired'
            else:
                print('verify_code update({}, {})'.format(counter, verified))
                response = USERS.update_item('id', account['id'], 'otp', secret + ':' + str(verified + 1))
    else:
        errmsg = 'The code is invalid or has expired'

    return errmsg

@APP.errorhandler(400)
def bad_request(error):
    """ Handle HTTP Bad Request error
    """
    if error.description:
        return make_response(jsonify({'error': str(error.description)}), 400)
    else:
        return make_response(jsonify({'error': str(error)}), 400)

@APP.errorhandler(401)
def unauthorized(error):
    """ Handle HTTP Unauthorized error
    """
    if error.description:
        return make_response(jsonify({'error': str(error.description)}), 401)
    else:
        return make_response(jsonify({'error': str(error)}), 401)

@APP.errorhandler(403)
def forbidden(error):
    """ Handle HTTP Forbidden error
    """
    if error.description:
        return make_response(jsonify({'error': str(error.description)}), 403)
    else:
        return make_response(jsonify({'error': str(error)}), 403)

@APP.errorhandler(404)
def not_found(error):
    """ Handle HTTP Not Found error
    """
    if error.description:
        return make_response(jsonify({'error': str(error.description)}), 404)
    else:
        return make_response(jsonify({'error': str(error)}), 404)

@APP.errorhandler(405)
def not_allowed(error):
    """ Handle HTTP Method Not Allowed error
    """
    if error.description:
        return make_response(jsonify({'error': str(error.description)}), 405)
    else:
        return make_response(jsonify({'error': str(error)}), 405)

@APP.errorhandler(409)
def resource_exists(error):
    """ Handle HTTP Conflict error
    """
    if error.description:
        return make_response(jsonify({'error': str(error.description)}), 409)
    else:
        return make_response(jsonify({'error': str(error)}), 409)

@APP.errorhandler(422)
def unprocessable_entity(error):
    """ Handle HTTP Unprocessable entity error
    """
    if error.description:
        return make_response(jsonify({'error': str(error.description)}), 422)
    else:
        return make_response(jsonify({'error': str(error)}), 422)

@APP.errorhandler(500)
def server_error(error):
    """ Handle HTTP Server error
    """
    if 'description' in error:
        return make_response(jsonify({'error': str(error.description)}), 500)
    else:
        return make_response(jsonify({'error': str(error)}), 500)

@APP.route('/google3dd7b0647e1f4d7a.html')
def google_site_verify():
    """ Google site verification
    """
    return render_template('google3dd7b0647e1f4d7a.html')

@APP.route('/pinterest-98ea8.html')
def pinterst_site_verify():
    """ Pinterst site verification
    """
    return render_template('pinterest-98ea8.html')

@APP.route('/robots.txt')
def robots_txt():
    """ Robots.txt file
    """
    return render_template('robots.txt')

@APP.route('/')
@APP.route('/index')
def index():
    """ Show main landing page
    """
    return render_template('index.html', search=RECIPE_LIST)

@APP.route('/api/server.info')
def server_info():
    """ Return server status information
    """
    url_fields = urlparse(request.url)
    timestamp = int((datetime.now(tz=pytz.utc) -
                     datetime(1970, 1, 1, tzinfo=pytz.utc)).total_seconds())
    uptime = time.strftime("%H:%M:%S", time.gmtime(timestamp - SERVER_START))
    return jsonify({'server': url_fields.netloc, 'version': SERVER_VERSION, 'uptime': uptime})

@APP.route('/api/message.email')
#@login_required
def message_email():
    """ Send an email message
    """
    title = get_parameter(request, 'recipe')
    email = get_parameter(request, 'email')
    if title is None or email is None:
        abort(400, 'Invalid input, recipe and email expected')
    userid = generate_user_id(CONFIG.get('user_id_hmac'), email)
    account = USERS.get_item('id', userid)
    if account and 'error' not in account:
        recipe = RECIPE_MANAGER.get_recipe(title)
        if recipe is not None and 'error' not in recipe:
            image = recipe.get('image')
            if image:
                if image.endswith('_hd'):
                    image = image.replace('_hd', '_small')
                else:
                    image = image.replace(".jpg", "_small.jpg")
            link = url_for('recipes', recipe=title.replace(' ', '%20'), _external=True)
            user = account.get('user') or email
            inviter = "Frosty" #current_user.get_user()
            intro = u'{} has shared a recipe for {} with you.'.format(inviter, title)
            send_email(email, title, 'recipe',
                       user=user, intro=intro, link=link, image=image, signature='Enjoy<br />,{}'.format(inviter))
            return jsonify({'message.email': email, 'status': 'ok'})
    abort(404, 'Recipient or recipe not found')

@APP.route('/api/recipe.post')
#@login_required
def recipe_post():
    """ Post one or more new recipes
    """
    recipe = get_parameter(request, 'recipe')
    if recipe is None:
        abort(400, 'Invalid input, recipe expected')
    userid = generate_user_id(CONFIG.get('user_id_hmac'), current_user.get_email())
    account = USERS.get_item('id', userid)
    if account and 'error' not in account:
        RECIPE_MANAGER.load_recipes(recipe)
        return jsonify({'recipe.post': recipe, 'status': 'ok'})
    abort(404, 'Recipe not found')

@APP.route('/search', methods=['GET'])
def search_recipes():
    """ Search recipes
    """
    # GET - query parameters are in the URL
    if request.method == 'GET':
        query = get_parameter(request, 'query')
    if query:
        phrases = query.split()
        if len(phrases) == 1:
            matches = RECIPE_MANAGER.match_recipe_by_category(query)
            matches = matches.union(RECIPE_MANAGER.match_recipe_by_title(query))
        elif len(phrases) > 1:
            matches = set()
            for phrase in phrases:
                wmatch = RECIPE_MANAGER.match_recipe_by_category(phrase)
                wmatch = wmatch.union(RECIPE_MANAGER.match_recipe_by_title(phrase))
                matches = matches.intersection(wmatch) if matches else wmatch
        title = 'Search Results ({}, found {})'.format(query, len(matches))
        if matches:
            html = RECIPE_MANAGER.get_recipe_list(matches)
        else:
            html = '<br />\n<p>No recipes matching search phrase "{}". Try the recipe navigator or another search.</p>\n'.format(query)
            html += RECIPE_MANAGER.get_sample_recipes()
    else:
        html = '<br />\n<p>Search for recipes by name and category or try the navigator.</p>\n'
        html += RECIPE_MANAGER.get_sample_recipes()

    return render_template('search.html', search=RECIPE_LIST, results=html)

@APP.route('/recipes', methods=['GET'])
def recipes():
    """ Show recipes
    """
    if current_user.is_authenticated:
        userid = current_user.get_id()
    else:
        userid = get_ip_address(request)

    recipe = get_parameter(request, 'recipe')
    if recipe is not None:
        EVENT_MANAGER.web_event('recipes', userid, **{"recipe": recipe})
        html = RECIPE_MANAGER.get_rendered_recipe(recipe)
        return render_template('recipes.html', search=RECIPE_LIST, recipe=html, title=recipe)

    EVENT_MANAGER.web_event('recipes', userid)
    html = RECIPE_MANAGER.get_latest_recipe()
    return render_template('recipes.html', search=RECIPE_LIST, recipe=html)

@APP.route('/gallery')
def gallery():
    """ Show gallery
    """
    category = get_parameter(request, 'category')
    if category:
        matches = RECIPE_MANAGER.match_recipe_by_category(category)
        html = RECIPE_MANAGER.get_rendered_gallery(matches)
        search = RECIPE_MANAGER.build_search_list(matches)
    else:
        html = RECIPE_MANAGER.get_rendered_gallery()
        search = RECIPE_LIST
    return render_template('gallery.html', search=search, gallery=html)

@APP.route('/upload', methods=['GET', 'POST'])
#@login_required
def upload():
    """ Upload an image with metadata
    """
    userid = generate_user_id(CONFIG.get('user_id_hmac'), current_user.get_email())
    account = USERS.get_item('id', userid)
    if not account or 'error' in account:
        return redirect(url_for('register', email=current_user.get_email()))
    if 'bucket' not in account:
        return redirect(url_for('profile', email=current_user.get_email()))
    path = userid + '/'
    form = UploadForm()
    if form.validate_on_submit():
        content_type = request.headers.get('Content-Type')
        if not (content_type and content_type.startswith('multipart/form-data')):
            abort(400, 'Missing or unsupported content type for upload')
        print(request.files['file'])
        # Handle multipart form encoded data
        if request.method == 'POST':
            content = request.files['file']
            if not content:
                abort(400, 'No file content for upload')
            if content.filename == '':
                abort(400, 'No file selected for upload')
            if not allowed_file(content.filename):
                abort(400, 'Unsupported file type for upload')
            path += secure_filename(content.filename)
            #params = {'file':content.filename, 'filename':path, 'identifier':group}

        if form.tags.data:
            tags = [tag.strip() for tag in form.tags.data.lower().split(',')]
        else:
            tags = []
        metadata = {'title': form.title.data,
                    'artform': form.artform.data,
                    'created': form.created.data,
                    'dimensions': form.dimensions.data,
                    'path': path,
                    'tags': tags}
        print(json.dumps(metadata))
        aws3 = S3()
        response = aws3.upload_data(content, account['bucket'], path)
        if 'error' in response:
            abort(400, response['error'])
    return render_template('upload.html', form=form)

@APP.route('/messages')
@login_required
def messages():
    """ Show messages
    """
    return render_template('messages.html')

@APP.route('/vault', methods=['GET', 'PATCH', 'POST', 'PUT'])
@fresh_login_required
def vault():
    """ Get or update the vault contents
    """
    userid = generate_user_id(CONFIG.get('user_id_hmac'), current_user.get_email())
    myvault = VAULT_MANAGER.get_vault(userid)
    if request.method == 'GET':
        if 'error' in myvault:
            html = VAULT_MANAGER.get_rendered_vault(None)
            mcf = '<div hidden id="mcf"></div>'
        else:
            mcf = '<div hidden id="mcf">' + myvault.get('mcf', '') + '</div>'
            box = get_parameter(request, 'box')
            if box is not None:
                mybox = myvault[box]
                html = '<div hidden id="safebox">' + json.dumps(mybox) + '</div>\n'
                html += '<div id="safebox-table"></div>\n'
            else:
                html = VAULT_MANAGER.get_rendered_vault(myvault)
        return render_template('vault.html', contents=html, mcf=mcf)

    elif request.method == 'PATCH' or request.method == 'PUT':
        if not request.json:
            abort(400, 'Invalid input, json expected')
        if 'error' in myvault:
            abort(404, myvault['error'])
        for key in myvault.keys():
            if key in request.json:
                myvault[key]['contents'] = request.json[key]
                response = VAULT_MANAGER.post_vault(userid, myvault)
                if 'error' in response:
                    abort(422, response['error'])
                return jsonify(response)

    elif request.method == 'POST':
        if not request.json:
            abort(400, 'Invalid input, json expected')
        if 'error' not in myvault:
            abort(409, 'Vault already exists')
        mcf = request.json.get('mcf')
        box = request.json.get('box')
        columns = request.json.get('columns')
        contents = request.json.get('contents') or ''
        title = request.json.get('title')
        icon = request.json.get('icon')
        if not mcf or not box or not columns or not isinstance(columns, list):
            abort(422, 'Missing box, columns or mcf')
        if not title:
            title = box[:1].upper() + box[1:]
        if not icon:
            icon = 'fa-key'
        myvault = {"mcf": mcf,
                   box: {"title": title, "icon": icon, "columns": columns, "contents": contents}}
        response = VAULT_MANAGER.post_vault(userid, myvault)
        if 'error' in response:
            abort(422, response['error'])
        return jsonify(response)

@APP.route('/privacy', methods=['GET'])
def privacy():
    """ Show privacy policy
    """
    return render_template('privacy.html')

@APP.route("/accept", methods=['GET', 'POST'])
def accept():
    """ Accept account invitation with emailed token and optional SMS code
    """
    errmsg = None
    form = AcceptForm()
    agent = {"ip": get_ip_address(request), "from": get_user_agent(request)}
    # GET - populate form with confirmation data
    if request.method == 'GET':
        email = get_parameter(request, 'email')
        token = get_parameter(request, 'token')
        action = get_parameter(request, 'action')
        if email is None or action is None or token is None:
            errmsg = 'Missing or invalid invitation'
        else:
            form.email.data = email
            form.token.data = token
            form.action.data = action
            validated, value = validate_timed_token(token, APP.config['SECRET_KEY'], action)
            if not validated or value != email:
                errmsg = 'The invitation link is invalid or has expired'
            else:
                userid = generate_user_id(CONFIG.get('user_id_hmac'), email)
                account = USERS.get_item('id', userid)
                if not account or 'error' in account:
                    errmsg = 'Unable to validate your credentials'
                else:
                    form.user.data = account.get('user')
                    form.phone.data = account.get('phone')
                if form.phone.data:
                    send_code(account, action)
        if errmsg:
            form.errors['Accept'] = [errmsg]
            EVENT_MANAGER.error_event('accept', email, errmsg, **agent)

    # POST - validate user credentials from the form and if successful they are logged in
    elif form.validate_on_submit():
        form = validate_credentials(form)
        if len(form.errors) < 1:
            return redirect(url_for('profile'))

    title = 'Accept Invitation'
    return render_template('accept.html', form=form, title=title)

@APP.route("/confirm", methods=['GET', 'POST'])
def confirm():
    """ Confirm user account or action (delete) with emailed token and optional SMS code
    """
    errmsg = None
    agent = {"ip": get_ip_address(request), "from": get_user_agent(request)}
    form = ConfirmForm()
    # GET - populate form with confirmation data
    if request.method == 'GET':
        email = get_parameter(request, 'email')
        token = get_parameter(request, 'token')
        action = get_parameter(request, 'action')
        if email is None or action is None or token is None:
            errmsg = 'Unable to validate your credentials'
        else:
            form.email.data = email
            form.token.data = token
            form.action.data = action
            validated, value = validate_timed_token(token, APP.config['SECRET_KEY'], action)
            if not validated or value != email:
                errmsg = 'The confirmation link is invalid or has expired'
            else:
                userid = generate_user_id(CONFIG.get('user_id_hmac'), email)
                account = USERS.get_item('id', userid)
                if not account or 'error' in account:
                    errmsg = 'Unable to validate your credentials'
        if errmsg:
            form.errors['Confirm'] = [errmsg]
            EVENT_MANAGER.error_event('confirm', email, errmsg, **agent)

    # POST - validate user credentials from the form and if successful they are logged in
    elif form.validate_on_submit():
        form = validate_credentials(form)
        if len(form.errors) < 1:
            return redirect(url_for('profile'))

    if action == 'register':
        title = 'Confirm Account'
    elif action == 'delete':
        title = 'Delete Account'
    else:
        title = 'Confirm'
    return render_template('confirm.html', form=form, title=title)


@APP.route("/login", methods=['GET', 'POST'])
def login():
    """ Login to user account with email and password
    """
    agent = {"ip": get_ip_address(request), "from": get_user_agent(request)}
    form = LoginForm()
    # GET - populate form with email if passed
    if request.method == 'GET':
        form.email.data = get_parameter(request, 'email')

    # POST - validate form data
    elif form.validate_on_submit():
        # Login and validate the user.
        email = form.email.data
        userid = generate_user_id(CONFIG.get('user_id_hmac'), email) if email else 'Unknown'
        account = USERS.get_item('id', userid)
        if not account or 'error' in account:
            errmsg = 'Unable to validate your credentials'
            form.errors['Login'] = [errmsg]
            EVENT_MANAGER.error_event('login', email, errmsg, **agent)
            form.password.data = ''
            return render_template('login.html', form=form)
        session = SESSIONS.get_item('id', userid)
        if 'error' in session:
            session['id'] = userid
            session['email'] = email
            session['user'] = account.get('user')

        failures = check_account_lock(session)
        session['failures'] = failures
        if failures > MAX_FAILURES:
            errmsg = 'Your account is locked'
            form.errors['Login'] = [errmsg]
            form.password.data = ''
            EVENT_MANAGER.error_event('login', userid, errmsg, **agent)
            return render_template('login.html', form=form)

        # Check password
        mcf = derive_key(form.password.data, account['mcf'])
        if mcf != account.get('mcf'):
            errmsg = failed_account_attempt(session, failures)
            form.errors['Login'] = [errmsg]
            form.password.data = ''
            EVENT_MANAGER.error_event('login', userid, errmsg, **agent)
            return render_template('login.html', form=form)

        if account.get('authentication') == 'password':
            user_authenticated(userid, account, session, agent, 'login', form.remember.data)
        elif account.get('authentication') == 'password:sms':
            target = get_parameter(request, 'next')
            if target is None or not is_safe_url(target):
                target = 'index'
            return redirect(url_for('verify', next=target, action='login', email=email))
        else:
            print('unknown authentication')

        return redirect_back('index')
    return render_template('login.html', form=form)

@APP.route("/logout")
@login_required
def logout():
    """ Logout of user account
    """
    SESSIONS.delete_item('id', current_user.get_id())
    EVENT_MANAGER.web_event('logout', current_user.get_id())
    logout_user()
    return redirect(url_for('index'))

@APP.route('/verify', methods=['GET', 'POST'])
def verify():
    """ 2FA verification
    """
    form = VerifyForm()
    agent = {"ip": get_ip_address(request), "from": get_user_agent(request)}

    # Send a code to our user when they GET this page, unless there are errors
    if request.method == 'GET':
        email = get_parameter(request, 'email') or current_user.get_email()
        form.email.data = email
        action = get_parameter(request, 'action')
        form.action.data = action
        userid = generate_user_id(CONFIG.get('user_id_hmac'), email) if email else 'Unknown'
        account = USERS.get_item('id', userid)
        if not account or 'error' in account:
            return redirect(url_for('register', email=email))
        if 'errors' not in form:
            if not send_code(account, action):
                form.errors['Verify'] = ['Unable to validate your credentials']
            else:
                flash('Verification code has been sent')

    # POST - validate form data
    elif form.validate_on_submit():
        action = form.action.data
        agent['verify'] = action
        email = form.email.data
        if not current_user.is_authenticated and (action == 'enable' or action == 'disable'):
            return redirect(url_for('login', email=email))
        if current_user.get_email() != email:
            print('verify emails mismatch {} {}'.format(current_user.get_email(), email))
        code = form.code.data
        userid = generate_user_id(CONFIG.get('user_id_hmac'), email) if email else 'Unknown'
        account = USERS.get_item('id', userid)
        if not account or 'error' in account:
            return redirect(url_for('register', email=email))
        authentication = account.get('authentication')
        session = SESSIONS.get_item('id', userid)
        if 'error' in session:
            session['id'] = userid
            session['email'] = email
            session['user'] = account.get('user')

        failures = check_account_lock(session)
        session['failures'] = failures
        if failures > MAX_FAILURES:
            errmsg = 'Your account is locked'
            form.errors['Verify'] = [errmsg]
            EVENT_MANAGER.error_event('verify', userid, errmsg, **agent)
            return render_template('verify.html', form=form)

        if authentication == 'password:authy':
            if code == '123456':
                verified = True
            #verified = verify_authy_token(user.authy_id, str(user_entered_code)).ok()
        elif authentication == 'password:sms':
            errmsg = verify_code(account, code)
        elif authentication == 'password' and 'phone' in account and action == 'enable':
            errmsg = verify_code(account, code)

        if errmsg is None:
            if action == 'login':
                user_authenticated(userid, account, session, agent, 'login')
            elif action == 'disable':
                response = USERS.update_item('id', userid, 'authentication', 'password')
            elif action == 'enable':
                response = USERS.update_item('id', userid, 'authentication', 'password:sms')
            return redirect_back('profile')
        else:
        # Code verification failed, update account lock and errmsg as needed
            errmsg = failed_account_attempt(session, failures, errmsg)
            form.errors['Verify'] = [errmsg]
            form.code.data = ''
            EVENT_MANAGER.error_event('verify', userid, errmsg, **agent)

    if action == 'enable':
        title = 'Verify Enable 2FA'
    elif action == 'disable':
        title = 'Verify Disable 2FA'
    elif action == 'login':
        title = 'Verify Login'
    elif action == 'delete':
        title = 'Verify Delete'
    else:
        title = 'Verify'
    return render_template('verify.html', form=form, title=title)

@APP.route("/profile", methods=['GET', 'POST'])
@login_required
def profile():
    """ Show user account profile
    """
    userid = generate_user_id(CONFIG.get('user_id_hmac'), current_user.get_email())
    account = USERS.get_item('id', userid)
    if not account or 'error' in account:
        return redirect(url_for('register', email=current_user.get_email()))
    session = SESSIONS.get_item('id', userid)
    if 'logins' in session:
        account['logins'] = session['logins']
    return render_template('profile.html', account=account)

@APP.route("/headlines", methods=['GET', 'POST'])
@login_required
def headlines():
    """ Show headlines
    """
    userid = generate_user_id(CONFIG.get('user_id_hmac'), current_user.get_email())
    account = USERS.get_item('id', userid)
    if not account or 'error' in account:
        return redirect(url_for('register', email=current_user.get_email()))
    return render_template('profile.html', account=account)

@APP.route("/change", methods=['GET', 'POST'])
@login_required
def change():
    """ Change user account password
    """
    form = ChangePasswordForm()
    # GET - populate form with email if passed
    if request.method == 'GET':
        form.email.data = current_user.get_email()

    # POST - validate form data
    elif form.validate_on_submit():
        agent = {"ip": get_ip_address(request), "from": get_user_agent(request)}
        email = form.email.data
        userid = generate_user_id(CONFIG.get('user_id_hmac'), email) if email else 'Unknown'
        account = USERS.get_item('id', userid)
        old_mcf = account.get('mcf')
        mcf = derive_key(form.oldpassword.data, old_mcf)
        if mcf != old_mcf:
            errmsg = 'Unable to validate your credentials'
            form.errors['Change'] = [errmsg]
            form.oldpassword.data = ''
            form.password.data = ''
            form.confirm.data = ''
            EVENT_MANAGER.error_event('change', userid, errmsg, **agent)
        else:
            mcf = derive_key(form.password.data)
            response = USERS.update_item('id', userid, 'mcf', mcf)
            if 'error' in response:
                form.errors['Change'] = [response['error']]
                EVENT_MANAGER.error_event('change', userid, response['error'], **agent)
            else:
                flash('Your password has been changed')
                EVENT_MANAGER.web_event('change', userid, **agent)
                return redirect(url_for('profile'))
    return render_template('change.html', form=form)

@APP.route("/resend", methods=['GET', 'POST'])
def resend():
    """ Regenerate and send a new token and/or code
    """
    errmsg = None
    form = ResendForm()
    # GET - populate form with email if passed
    if request.method == 'GET':
        if current_user.is_authenticated:
            email = current_user.get_email()
        else:
            email = get_parameter(request, 'email')
        action = get_parameter(request, 'action')
        if email is None or action is None:
            errmsg = 'Invalid or missing parameters'
        else:
            form.email.data = email
            form.action.data = action
            form.phone.data = get_parameter(request, 'phone')

    # POST - validate form data
    elif form.validate_on_submit():
        email = form.email.data
        action = form.action.data
        userid = generate_user_id(CONFIG.get('user_id_hmac'), email) if email else 'Unknown'
        account = USERS.get_item('id', userid)
        if not account or 'error' in account:
            errmsg = 'Unable to validate your credentials'
        else:
            EVENT_MANAGER.web_event('resend', userid, **{action: action})
            if action == 'invite':
                token = generate_timed_token(email, APP.config['SECRET_KEY'], action)
                link = url_for(action, email=email, token=token, action=action, _external=True)
                user = account.get('user') or email
                intro = 'You have requested a new {} token.'.format(action)
                send_email(email, 'Resend Token', 'resend',
                           user=user, intro=intro, token=token, link=link)
                flash('A new confirmation code has been sent to ' + email)
                return redirect(url_for('confirm', email=email, action=action))
            elif action[:6] == 'verify':
                return redirect(url_for('verify', email=email, action=action[7:]))

    if errmsg:
        form.errors['Resend'] = [errmsg]
        agent = {"ip": get_ip_address(request), "from": get_user_agent(request)}
        EVENT_MANAGER.error_event('resend', email, errmsg, **agent)

    return render_template('resend.html', form=form)

@APP.route("/invite", methods=['GET', 'POST'])
@login_required
def invite():
    """ Invite a new user to join by providing an email address and phone number for them.
        An invitation is emailed to the user with a temporary password and a one time code
        is sent via text message to the phone number.
    """
    form = InviteForm()
    if form.validate_on_submit():
        email = form.email.data
        user = form.user.data
        phone = form.phone.data
        if check_phone(phone):
            secret = generate_otp_secret()
        else:
            secret = None
            phone = None
        userid = generate_user_id(CONFIG.get('user_id_hmac'), email)
        account = USERS.get_item('id', userid)
        if account and 'error' not in account:
            errmsg = 'Email address already in use'
            form.errors['Invite'] = [errmsg]
            EVENT_MANAGER.error_event('invite', current_user.get_id(), errmsg, **{"email": email})
            return render_template('invite.html', form=form)
        password = generate_random58_id(12)
        counter = generate_random_int()
        info = {'id': userid,
                'email': email,
                'phone': phone,
                'user': user,
                'authentication': 'password',
                'mcf': preset_password(email, password),
                'otp': secret + ':' + str(counter),
                'created': 'invited: ' + datetime.utcnow().strftime('%Y-%m-%d')
               }
        USERS.put_item(info)
        action = 'invite'
        token = generate_timed_token(email, APP.config['SECRET_KEY'], action)
        link = url_for('accept', email=email, token=token, action=action, _external=True)
        inviter = current_user.get_user()
        if not phone:
            code = generate_hotp_code(secret, counter)
        else:
            code = None
        intro = u'{} has Invited you to join the Frosty Web community.'.format(inviter)
        send_email(email, 'Accept Invitation', 'invite',
                   user=user, intro=intro, link=link, password=password, code=code)
        flash('{} has been invited'.format(user))
        EVENT_MANAGER.web_event('invite', current_user.get_id(), **{"email": email})
        return redirect(url_for('profile'))
    return render_template('invite.html', form=form)

@APP.route("/forgot", methods=['GET', 'POST'])
def forgot():
    """ Request a password reset
    """
    errmsg = None
    if current_user.is_authenticated:
        return redirect(url_for('profile'))

    # GET - populate form with email if passed
    form = ForgotPasswordForm()
    if request.method == 'GET':
        form.email.data = get_parameter(request, 'email')

    # POST - validate form data
    elif form.validate_on_submit():
        agent = {"ip": get_ip_address(request), "from": get_user_agent(request)}
        email = form.email.data
        agent['email'] = email
        userid = generate_user_id(CONFIG.get('user_id_hmac'), email) if email else 'Unknown'
        account = USERS.get_item('id', userid)
        if not account or 'error' in account:
            errmsg = 'Unable to validate your credentials'
        else:
            password = generate_random58_id(12)
            reset_mcf = preset_password(email, password)
            response = USERS.update_item('id', userid, 'reset_mcf', reset_mcf)
            if 'error' in response:
                errmsg = 'Request failed'
                EVENT_MANAGER.error_event('forgot', userid, response['error'], **agent)
            else:
                token = generate_timed_token(email, APP.config['SECRET_KEY'], 'reset')
                link = url_for('reset', email=email, token=token, action='reset', _external=True)
                intro = 'You have requested a password reset.'
                send_email(email, 'Reset Password', 'reset',
                           user=account.get('user'), intro=intro, link=link, password=password)
                EVENT_MANAGER.web_event('forgot', userid, **agent)
                return redirect(url_for('reset', email=email, token=token, action='reset'))
    if errmsg:
        form.errors['Forgot'] = [errmsg]
        EVENT_MANAGER.error_event('forgot', userid, errmsg, **agent)
    return render_template('forgot.html', form=form)

@APP.route("/reset", methods=['GET', 'POST'])
def reset():
    """ Reset user password with emailed temporary password and token plus SMS/push token for 2FA
    """
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    mfa = None
    agent = {"ip": get_ip_address(request), "from": get_user_agent(request)}
    form = ResetPasswordForm()
    # GET - populate form with required parameters (these are all hidden fields)
    if request.method == 'GET':
        email = get_parameter(request, 'email')
        token = get_parameter(request, 'token')
        action = get_parameter(request, 'action')
        userid = generate_user_id(CONFIG.get('user_id_hmac'), email) if email else 'Unknown'
        if email is None or token is None or action != 'reset':
            EVENT_MANAGER.error_event('reset', userid, 'Invalid parameters', **agent)
            abort(403, 'Unable to validate your credentials')
        account = USERS.get_item('id', userid)
        if 'error' in account:
            EVENT_MANAGER.error_event('reset', userid, 'Unregistered email', **agent)
            abort(403, 'Unable to validate your credentials')
        if 'reset_mcf' not in account:
            form.errors['Reset'] = ['Unable to validate your credentials']
            EVENT_MANAGER.error_event('reset', userid, 'No pending reset request', **agent)
            #delay
            return render_template('reset.html', form=form)
        form.email.data = email
        form.token.data = token
        form.action.data = action

        # Send a code to our user when they GET this page when using SMS based 2FA
        if account.get('authentication') == 'password:sms' and 'phone' in account:
            mfa = True
            send_code(account, action)

    # POST - validate user credentials from the form and if successful they are logged in
    elif form.validate_on_submit():
        form = validate_credentials(form)
        if len(form.errors) < 1:
            return redirect(url_for('profile'))

    return render_template('reset.html', form=form, mfa=mfa)

@APP.route("/register", methods=['GET', 'POST'])
def register():
    """ Register a new user account
    """
    if current_user.is_authenticated:
        return redirect(url_for('profile'))
    form = RegistrationForm()

    # GET - populate form with email, name and phone if passed
    if request.method == 'GET':
        form.email.data = get_parameter(request, 'email')
        form.user.data = get_parameter(request, 'user')
        form.phone.data = get_parameter(request, 'phone')
    elif form.validate_on_submit():
        agent = {"ip": get_ip_address(request), "from": get_user_agent(request)}
        email = form.email.data
        phone = form.phone.data
        token = form.token.data
        user = sanitize_name(form.user.data)
        userid = generate_user_id(CONFIG.get('user_id_hmac'), email) if email else 'Unknown'
        account = USERS.get_item('id', userid)
        if 'error' not in account:
            errmsg = 'Email address already in use'
            form.errors['Register'] = [errmsg]
            EVENT_MANAGER.error_event('register', userid, errmsg, **agent)
            form.password.data = ''
            form.confirm.data = ''
            form.token.data = ''
            return render_template('register.html', form=form)
        validated, value = validate_timed_token(token, APP.config['SECRET_KEY'], 'register')
        if validated and value == email:
            print('registering new user')
        else:
            errmsg = 'Invalid or expired token'
            form.errors['Register'] = [errmsg]
            form.password.data = ''
            form.confirm.data = ''
            form.token.data = ''
            EVENT_MANAGER.error_event('register', userid, errmsg, **agent)
            return redirect(url_for('resend', email=email, action='register'))
        # Create json for new user
        if check_phone(phone):
            secret = generate_otp_secret()
        else:
            secret = None
            phone = None
        counter = generate_random_int()
        info = {'id': generate_user_id(CONFIG.get('user_id_hmac'), email),
                'email': email,
                'phone': phone,
                'user': user,
                'authentication': 'password',
                'mcf': derive_key(form.password.data),
                'otp': secret + ':' + str(counter),
                'created': 'pending: ' + datetime.utcnow().strftime('%Y-%m-%d')
               }
        user = User(email, user)
        user.is_authenticated = False
        user.is_active = False
        USERS.put_item(info)
        token = generate_timed_token(email, APP.config['SECRET_KEY'], 'confirm')
        link = url_for('confirm', email=email, token=token, action='register', _external=True)
        if phone:
            code = generate_hotp_code(secret, counter)
        else:
            code = None
        intro = 'You have registered for a new account and need to confirm that it was really you.'
        send_email(email, 'Confirm Account', 'confirm',
                   user=user, intro=intro, link=link, code=code)
        flash('A confirmation email has been sent to ' + form.email.data)
        EVENT_MANAGER.web_event('register', userid, **agent)
    return render_template('register.html', form=form)

def handle_sigterm(signum, frame):
    """ Catch SIGTERM and SIGINT and stop the server by raising an exception
    """
    if frame:
        print(signum)
    raise SystemExit('Killed')

def main():
    """ Main for localhost testing via manage.py (start, stop, restart)
    """
    reason = 'Normal'
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(('8.8.8.8', 1))  # Connecting to a Google UDP address
        host_ip = sock.getsockname()[0]
        sock.close()
        signal.signal(signal.SIGINT, handle_sigterm)
        signal.signal(signal.SIGTERM, handle_sigterm)

        print('Web server starting: %s:%d' % (host_ip, 8080))
        EVENT_MANAGER.log_event({'type': 'server.start', 'ip': host_ip})
        APP.run(debug=False, host='0.0.0.0', port=8080, threaded=True)
    except (KeyboardInterrupt, SystemExit):
        reason = 'Stopped'
    except (EnvironmentError, RuntimeError) as err:
        LOGGER.error(err)
        reason = str(err)
    except EndpointConnectionError as err:
        LOGGER.error(err)
        reason = str(err)
    EVENT_MANAGER.log_event({'type': 'server.stop', 'exit': reason})
    EVENT_MANAGER.flush_events()
    print(reason)

if __name__ == '__main__':
    LOGGER.setLevel(logging.ERROR)
    file_handler = logging.FileHandler("cyberfrosty.log")
    formatter = logging.Formatter('%(asctime)s %(levelname)s cyberfrosty:%(funcName)s %(message)s')
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)
    #print(USERS.load_table('users.json'))
    main()
