import unicodecsv as csv
import json
import requests
import datetime
import io

from flask import request, url_for, render_template, redirect, \
                  jsonify, flash, make_response, Response, g
from flask.ext.login import current_user, login_required
from flask.ext.cors import cross_origin
from urlparse import urljoin

from formspree import settings
from formspree.app import DB
from formspree.utils import request_wants_json, jsonerror, IS_VALID_EMAIL
from helpers import ordered_storage, referrer_to_path, remove_www, \
                    referrer_to_baseurl, sitewide_file_check, \
                    HASH, EXCLUDE_KEYS
from models import Form, Submission


def thanks():
    return render_template('forms/thanks.html')


@cross_origin(allow_headers=['Accept', 'Content-Type', 'X-Requested-With'])
@ordered_storage
def send(email_or_string):
    '''
    Main endpoint, finds or creates the form row from the database,
    checks validity and state of the form and sends either form data
    or verification to email.
    '''

    g.log = g.log.bind(target=email_or_string)

    if request.method == 'GET':
        if request_wants_json():
            return jsonerror(405, {'error': "Please submit POST request."})
        else:
            return render_template('info.html',
                                   title='Form should POST',
                                   text='Make sure your form has the <span class="code"><strong>method="POST"</strong></span> attribute'), 405

    host = referrer_to_path(request.referrer)
    if not host:
        if request_wants_json():
            return jsonerror(400, {'error': "Invalid \"Referrer\" header"})
        else:
            return render_template('error.html',
                                   title='Unable to submit form',
                                   text='<p>Make sure you open this page through a web server, Formspree will not work in pages browsed as HTML files. Also make sure that you\'re posting to <b>https://</b>{host}.</p><p>For geeks: could not find the "Referrer" header.</p>'.format(host=request.url.split('//')[1])), 400

    g.log = g.log.bind(host=host, wants='json' if request_wants_json() else 'html')

    g.log.info('Received submission.')
    if not IS_VALID_EMAIL(email_or_string):
        # in this case it can be a hashid identifying a
        # form generated from the dashboard
        hashid = email_or_string
        form = Form.get_with_hashid(hashid)

        if form:
            if form.disabled:
                # owner has disabled the form, so it should not receive any submissions
                if request_wants_json():
                    return jsonerror(403, {'error': 'Form not active'})
                else:
                    return render_template('error.html',
                                           title='Form not active',
                                           text='The owner of this form has disabled this form and it is no longer accepting submissions. Your submissions was not accepted'), 403
            email = form.email

            if not form.host:
                # add the host to the form
                form.host = host
                DB.session.add(form)
                DB.session.commit()

                # it is an error when
                #   form is sitewide, but submission came from a host rooted somewhere else, or
                #   form is not sitewide, and submission came from a different host
            elif (not form.sitewide and form.host != host) or (
                   form.sitewide and (
                     not host.startswith(form.host) and \
                     not remove_www(host).startswith(form.host)
                   )
                 ):
                g.log.info('Submission rejected. From a different host than confirmed.')
                if request_wants_json():
                    return jsonerror(403, {
                       'error': "Submission from different host than confirmed",
                       'submitted': host, 'confirmed': form.host
                    })
                else:
                    return render_template('error.html',
                                           title='Check form address',
                                           text='This submission came from "%s" but the form was\
                                                 confirmed for address "%s"' % (host, form.host)), 403
        else:
            # no form row found. it is an error.
            g.log.info('Submission rejected. No form found for this target.')
            if request_wants_json():
                return jsonerror(400, {'error': "Invalid email address"})
            else:
                return render_template('error.html',
                                       title='Check email address',
                                       text='Email address %s is not formatted correctly' \
                                            % str(email_or_string)), 400
    else:
        # in this case, it is a normal email
        email = email_or_string.lower()

        # get the form for this request
        form = Form.query.filter_by(hash=HASH(email, host)).first() \
               or Form(email, host) # or create it if it doesn't exists
        if form.disabled:
            g.log.info('submission rejected. Form is disabled.')
            if request_wants_json():
                return jsonerror(403, {'error': 'Form not active'})
            else:
                return render_template('error.html',
                                       title='Form not active',
                                       text='The owner of this form has disabled this form and it is no longer accepting submissions. Your submissions was not accepted'), 403

    # If form exists and is confirmed, send email
    # otherwise send a confirmation email
    received_data = request.form or request.get_json() or {}
    if form.confirmed:
        status = form.send(received_data, request.referrer)
    else:
        status = form.send_confirmation(received_data)

    # Respond to the request accordingly to the status code
    if status['code'] == Form.STATUS_EMAIL_SENT:
        if request_wants_json():
            return jsonify({'success': "email sent", 'next': status['next']})
        else:
            return redirect(status['next'], code=302)
    elif status['code'] == Form.STATUS_EMAIL_EMPTY:
        if request_wants_json():
            return jsonerror(400, {'error': "Can't send an empty form"})
        else:
            return render_template('error.html',
                                   title='Can\'t send an empty form',
                                   text=str('<p>Make sure you have placed the <a href="http://www.w3schools.com/tags/att_input_name.asp" target="_blank">"name" attribute</a> in all your form elements. Also, to prevent empty form submissions, take a look at the <a href="http://www.w3schools.com/tags/att_input_required.asp" target="_blank">"required" property</a> or <a href="https://developer.mozilla.org/en-US/docs/Web/HTML/Element/input" target="_blank">see more HTML form customization info</a>.</p><p><a href="%s">Return to form</a></p>' % request.referrer)), 400
    elif status['code'] == Form.STATUS_CONFIRMATION_SENT or \
         status['code'] == Form.STATUS_CONFIRMATION_DUPLICATED:

        if request_wants_json():
            return jsonify({'success': "confirmation email sent"})
        else:
            return render_template('forms/confirmation_sent.html',
                email=email,
                host=host,
                resend=status['code'] == Form.STATUS_CONFIRMATION_DUPLICATED
            )
    elif status['code'] == Form.STATUS_OVERLIMIT:
        if request_wants_json():
            return jsonify({'error': "form over quota"})
        else:
            return render_template('error.html', title='Form over quota', text='It looks like this form is getting a lot of submissions and ran out of its quota. Try contacting this website through other means or try submitting again later.'), 402

    elif status['code'] == Form.STATUS_REPLYTO_ERROR:
        if request_wants_json():
            return jsonerror(500, {'error': "_replyto or email field has not been sent correctly"})
        else:
            return render_template('error.html', title='Invalid email address', text='You entered <span class="code">{address}</span>. That is an invalid email address. Please correct the form and try to submit again <a href="{back}">here</a>.<p style="font-size: small">This could also be a problem with the form. For example, there could be two fields with <span class="code">_replyto</span> or <span class="code">email</span> name attribute. If you suspect the form is broken, please contact the form owner and ask them to investigate</p>'''.format(address=status['address'], back=status['referrer'])), 400

    # error fallback -- shouldn't happen
    if request_wants_json():
        return jsonerror(500, {'error': "Unable to send email"})
    else:
        return render_template('error.html',
                               title='Unable to send email',
                               text='Unable to send email. If you can, please send the link to your form and the error information to  <b>{email}</b>. And send them the following: <p><pre><code>{message}</code></pre></p>'.format(message=json.dumps(status), email=settings.CONTACT_EMAIL)), 500


def resend_confirmation(email):
    g.log = g.log.bind(email=email, host=request.form.get('host'))
    g.log.info('Resending confirmation.')

    # the first thing to do is to check the captcha
    r = requests.post('https://www.google.com/recaptcha/api/siteverify', data={
        'secret': settings.RECAPTCHA_SECRET,
        'response': request.form['g-recaptcha-response'],
        'remoteip': request.remote_addr
    })
    if r.ok and r.json().get('success'):
        # then proceed to check if this email is listed on SendGrid's bounces
        r = requests.get('https://api.sendgrid.com/api/bounces.get.json',
            params={
                'email': email,
                'api_user': settings.SENDGRID_USERNAME,
                'api_key': settings.SENDGRID_PASSWORD
            }
        )
        if r.ok and len(r.json()) and 'reason' in r.json()[0]:
            # tell the user to verify his mailbox
            reason = r.json()[0]['reason']
            g.log.info('Email is blocked on SendGrid. Telling the user.')
            if request_wants_json():
                resp = jsonify({'error': "Verify your mailbox, we can't reach it.", 'reason': reason})
            else:
                resp = make_response(render_template('info.html',
                    title='Verify the availability of your mailbox',
                    text="We encountered an error when trying to deliver the confirmation message to <b>" + email + "</b> at the first time we tried. For spam reasons, we will not try again until we are sure the problem is fixed. Here's the reason:</p><p><center><i>" + reason + "</i></center></p><p>Please make sure this problem is not happening still, then go to <a href='/unblock/" + email + "'>this page</a> to unblock your address.</p><p>After you have unblocked the address, please try to resend the confirmation again.</p>"
                ))
            return resp
        # ~~~
        # if there's no bounce, we proceed to resend the confirmation.

        form = Form.query.filter_by(hash=HASH(email, request.form['host'])).first()
        if not form:
            if request_wants_json():
                return jsonerror(400, {'error': "This form does not exist."})
            else:
                return render_template('error.html',
                                       title='Check email address',
                                       text='This form does not exist.'), 400
        form.confirm_sent = False
        status = form.send_confirmation()
        if status['code'] == Form.STATUS_CONFIRMATION_SENT:
            if request_wants_json():
                return jsonify({'success': "confirmation email sent"})
            else:
                return render_template('forms/confirmation_sent.html',
                    email=email,
                    host=request.form['host'],
                    resend=status['code'] == Form.STATUS_CONFIRMATION_DUPLICATED
                )

    # fallback response -- should happen only when the recaptcha is failed.
    g.log.warning('Failed to resend confirmation.')
    return render_template('error.html',
                           title='Unable to send email',
                           text='Please make sure you pass the <i>reCaptcha</i> test before submitting.'), 500


def unblock_email(email):
    if request.method == 'POST':
        g.log = g.log.bind(email=email)
        g.log.info('Unblocking email on SendGrid.')

        # check the captcha
        r = requests.post('https://www.google.com/recaptcha/api/siteverify', data={
            'secret': settings.RECAPTCHA_SECRET,
            'response': request.form['g-recaptcha-response'],
            'remoteip': request.remote_addr
        })
        if r.ok and r.json().get('success'):
            # then proceed to clear the bounce from SendGrid
            r = requests.post(
                'https://api.sendgrid.com/api/bounces.delete.json',
                data={
                    'email': email,
                    'api_user': settings.SENDGRID_USERNAME,
                    'api_key': settings.SENDGRID_PASSWORD
                }
            )
            if r.ok and r.json()['message'] == 'success':
                g.log.info('Unblocked address.')
                return render_template('info.html',
                                       title='Successfully unblocked email address!',
                                       text='You should be able to receive emails from Formspree again.')
            else:
                g.log.warning('Failed to unblock email on SendGrid.')
                return render_template('error.html',
                                       title='Failed to unblock address.',
                                       text=email + ' is not a valid address or was\'t blocked on our side.')

        # fallback response -- should happen only when the recaptcha is failed.
        g.log.warning('Failed to unblock email. reCaptcha test failed.')
        return render_template('error.html',
                               title='Unable to unblock email',
                               text='Please make sure you pass the <i>reCaptcha</i> test before submitting.'), 500

    elif request.method == 'GET':
        return render_template('forms/unblock_email.html', email=email), 200


def confirm_email(nonce):
    '''
    Confirmation emails point to this endpoint
    It either rejects the confirmation or
    flags associated email+host to be confirmed
    '''

    # get the form for this request
    form = Form.confirm(nonce)

    if not form:
        return render_template('error.html',
                               title='Not a valid link',
                               text='Confirmation token not found.<br />Please check the link and try again.'), 400

    else:
        return render_template('forms/email_confirmed.html', email=form.email, host=form.host)


@login_required
def forms():
    '''
    A reminder: this is the /forms endpoint, but for GET requests
    it is also the /dashboard endpoint.

    The /dashboard endpoint, the address gave by url_for('dashboard'),
    is the target of a lot of redirects around the app, but it can
    be changed later to point to somewhere else.
    '''

    # grab all the forms this user controls
    if current_user.upgraded:
        forms = current_user.forms.order_by(Form.id.desc()).all()
    else:
        forms = []

    if request_wants_json():
        return jsonify({
            'ok': True,
            'forms': [{
                'email': f.email,
                'host': f.host,
                'confirm_sent': f.confirm_sent,
                'confirmed': f.confirmed,
                'is_public': bool(f.hash),
                'url': '{S}/{E}'.format(
                    S=settings.SERVICE_URL,
                    E=f.hashid
                )
            } for f in forms]
        })
    else:
        return render_template('forms/list.html',
            enabled_forms=[form for form in forms if not form.disabled],
            disabled_forms=[form for form in forms if form.disabled]
        )


@login_required
def create_form():
    # create a new form

    if not current_user.upgraded:
        g.log.info('Failed to create form from dashboard. User is not upgraded.')
        return jsonerror(402, {'error': "Please upgrade your account."})

    if request.get_json():
        email = request.get_json().get('email')
        url = request.get_json().get('url')
        sitewide = request.get_json().get('sitewide')
    else:
        email = request.form.get('email')
        url = request.form.get('url')
        sitewide = request.form.get('sitewide')

    g.log = g.log.bind(email=email, url=url, sitewide=sitewide)

    if not IS_VALID_EMAIL(email):
        g.log.info('Failed to create form from dashboard. Invalid address.')
        if request_wants_json():
            return jsonerror(400, {'error': "The provided email address is not valid."})
        else:
            flash('The provided email address is not valid.', 'error')
            return redirect(url_for('dashboard'))

    g.log.info('Creating a new form from the dashboard.')

    email = email.lower() # case-insensitive
    form = Form(email, owner=current_user)
    if url:
        url = 'http://' + url if not url.startswith('http') else url
        form.host = referrer_to_path(url)

        # sitewide forms, verified with a file at the root of the target domain
        if sitewide:
            if sitewide_file_check(url, email):
                form.host = remove_www(referrer_to_path(urljoin(url, '/'))[:-1])
                form.sitewide = True
            else:
                return jsonerror(403, {'error': "Couldn't verify the file at %s." % url})

    DB.session.add(form)
    DB.session.commit()

    if form.host:
        # when the email and url are provided, we can automatically confirm the form
        # but only if the email is registered for this account
        for email in current_user.emails:
            if email.address == form.email:
                g.log.info('No need for email confirmation.')
                form.confirmed = True
                DB.session.add(form)
                DB.session.commit()
                break
        else:
            # in case the email isn't registered for this user
            # we automatically send the email confirmation
            form.send_confirmation()

    if request_wants_json():
        return jsonify({
            'ok': True,
            'hashid': form.hashid,
            'submission_url': settings.API_ROOT + '/' + form.hashid,
            'confirmed': form.confirmed
        })
    else:
        flash('Your new form endpoint was created!', 'success')
        return redirect(url_for('dashboard', new=form.hashid) + '#form-' + form.hashid)


@login_required
def sitewide_check():
    email = request.args.get('email')
    url = request.args.get('url')

    if sitewide_file_check(url, email):
        return '', 200
    else:
        return '', 404


@login_required
def form_submissions(hashid, format=None):
    if not current_user.upgraded:
        return jsonerror(402, {'error': "Please upgrade your account."})

    form = Form.get_with_hashid(hashid)

    for cont in form.controllers:
        if cont.id == current_user.id: break
    else:
        if request_wants_json():
            return jsonerror(403, {'error': "You do not control this form."})
        else:
            return redirect(url_for('dashboard'))

    submissions = form.submissions

    if not format:
        # normal request.
        if request_wants_json():
            return jsonify({
                'host': form.host,
                'email': form.email,
                'submissions': [dict(s.data, date=s.submitted_at.isoformat()) for s in submissions]
            })
        else:
            fields = set()
            for s in submissions:
                fields.update(s.data.keys())
            fields -= set(EXCLUDE_KEYS)

            return render_template('forms/submissions.html',
                form=form,
                fields=sorted(fields),
                submissions=submissions
            )
    elif format:
        # an export request, format can be json or csv
        if format == 'json':
            return Response(
                json.dumps({
                    'host': form.host,
                    'email': form.email,
                    'submissions': [dict(s.data, date=s.submitted_at.isoformat()) for s in submissions]
                }, sort_keys=True, indent=2),
                mimetype='application/json',
                headers={
                    'Content-Disposition': 'attachment; filename=form-%s-submissions-%s.json' \
                                % (hashid, datetime.datetime.now().isoformat().split('.')[0])
                }
            )
        elif format == 'csv':
            out = io.BytesIO()
            fieldnames = set(field for sub in submissions for field in sub.data.keys())
            fieldnames = ['date'] + sorted(fieldnames)
            
            w = csv.DictWriter(out, fieldnames=fieldnames, encoding='utf-8')
            w.writeheader()
            for sub in submissions:
                w.writerow(dict(sub.data, date=sub.submitted_at.isoformat()))

            return Response(
                out.getvalue(),
                mimetype='text/csv',
                headers={
                    'Content-Disposition': 'attachment; filename=form-%s-submissions-%s.csv' \
                                % (hashid, datetime.datetime.now().isoformat().split('.')[0])
                }
            )


@login_required
def form_toggle(hashid):
    form = Form.get_with_hashid(hashid)

    # check that this request came from user dashboard to prevent XSS and CSRF
    referrer = referrer_to_baseurl(request.referrer)
    service = referrer_to_baseurl(settings.SERVICE_URL)
    if referrer != service:
        return render_template('error.html',
                               title='Improper Request',
                               text='The request you made is not valid.<br />Please visit your dashboard and try again.'), 400

    if form.owner_id != current_user.id:
        if form not in current_user.forms: #accounts for bug when form isn't assigned owner_id bc it was not created from dashboard
            return render_template('error.html',
                                  title='Wrong user',
                                  text='You aren\'t the owner of that form.<br />Please log in as the form owner and try again.'), 400
    if not form:
            return render_template('error.html',
                                   title='Not a valid form',
                                   text='That form does not exist.<br />Please check the link and try again.'), 400
    else:
        form.disabled = not form.disabled
        DB.session.add(form)
        DB.session.commit()
        if form.disabled:
            flash('Form successfully disabled', 'success')
        else:
            flash('Form successfully enabled', 'success')
        return redirect(url_for('dashboard'))


@login_required
def form_deletion(hashid):
    form = Form.get_with_hashid(hashid)

    # check that this request came from user dashboard to prevent XSS and CSRF
    referrer = referrer_to_baseurl(request.referrer)
    service = referrer_to_baseurl(settings.SERVICE_URL)
    if referrer != service:
        return render_template('error.html',
                               title='Improper Request',
                               text='The request you made is not valid.<br />Please visit your dashboard and try again.'), 400

    if form.owner_id != current_user.id:
        if form not in current_user.forms: #accounts for bug when form isn't assigned owner_id bc it was not created from dashboard
            return render_template('error.html',
                                  title='Wrong user',
                                  text='You aren\'t the owner of that form.<br />Please log in as the form owner and try again.'), 400
    if not form:
            return render_template('error.html',
                                   title='Not a valid form',
                                   text='That form does not exist.<br />Please check the link and try again.'), 400
    else:
        for submission in form.submissions:
            DB.session.delete(submission)
        DB.session.delete(form)
        DB.session.commit()
        flash('Form successfully deleted', 'success')
        return redirect(url_for('dashboard'))


@login_required
def submission_deletion(hashid, submissionid):
    submission = Submission.query.get(submissionid)
    form = Form.get_with_hashid(hashid)

    # check that this request came from user dashboard to prevent XSS and CSRF
    referrer = referrer_to_baseurl(request.referrer)
    service = referrer_to_baseurl(settings.SERVICE_URL)
    if referrer != service:
        return render_template('error.html',
                               title='Improper Request',
                               text='The request you made is not valid.<br />Please visit your dashboard and try again.'), 400

    if form.owner_id != current_user.id:
        if form not in current_user.forms: #accounts for bug when form isn't assigned owner_id bc it was not created from dashboard
            return render_template('error.html',
                                  title='Wrong user',
                                  text='You aren\'t the owner of that form.<br />Please log in as the form owner and try again.' + str(form.id)), 400
    if not submission:
        return render_template('error.html',
                              title='Not a valid submission',
                              text='That submission does not exist.<br />Please check the link and try again.'), 400
    elif submission.form_id != form.id:
        return render_template('error.html',
                              title='Not a valid submissions',
                              text='That submission does not match the form provided.<br />Please check the link and try again.'), 400
    else:
        DB.session.delete(submission)
        form.counter -= 1
        DB.session.add(form)
        DB.session.commit()
        flash('Submission successfully deleted', 'success')
        return redirect(url_for('form-submissions', hashid=hashid))
