import httpretty

from formspree import settings
from formspree.app import DB
from formspree.forms.models import Form
from formspree.users.models import User, Email

from formspree_test_case import FormspreeTestCase

ajax_headers = {
    'Referer': 'example.com',
    'X_REQUESTED_WITH': 'xmlhttprequest'
}


class FormPostsTestCase(FormspreeTestCase):

    def test_index_page(self):
        r = self.client.get('/')
        self.assertEqual(200, r.status_code)

    @httpretty.activate
    def test_submit_form(self):
        httpretty.register_uri(httpretty.POST, 'https://api.sendgrid.com/api/mail.send.json')
        self.client.post('/alice@example.com',
            headers = ajax_headers,
            data={'name': 'alice'}
        )
        self.assertEqual(1, Form.query.count())

    @httpretty.activate
    def test_second_form(self):
        httpretty.register_uri(httpretty.POST, 'https://api.sendgrid.com/api/mail.send.json')
        self.client.post('/bob@example.com',
            headers = ajax_headers,
            data={'name': 'bob'}
        )
        self.assertEqual(1, Form.query.count())

    @httpretty.activate    
    def test_fail_form_without_header(self):
        httpretty.register_uri(httpretty.POST, 'https://api.sendgrid.com/api/mail.send.json')
        httpretty.reset()

        no_referer = ajax_headers.copy()
        del no_referer['Referer']
        r = self.client.post('/bob@example.com',
            headers = no_referer,
            data={'name': 'bob'}
        )
        self.assertEqual(False, httpretty.has_request())
        self.assertNotEqual(200, r.status_code)

    @httpretty.activate    
    def test_fail_but_appears_to_have_succeeded_with_gotcha(self):
        httpretty.register_uri(httpretty.POST, 'https://api.sendgrid.com/api/mail.send.json')

        # manually confirm
        r = self.client.post('/carlitos@example.com',
            headers = {'Referer': 'http://carlitos.net/'},
            data={'name': 'carlitos'}
        )
        f = Form.query.first()
        f.confirm_sent = True
        f.confirmed = True
        DB.session.add(f)
        DB.session.commit()

        httpretty.reset()
        r = self.client.post('/carlitos@example.com',
            headers = {'Referer': 'http://carlitos.net/'},
            data={'name': 'Real Stock', '_gotcha': 'The best offers.'}
        )
        self.assertEqual(False, httpretty.has_request())
        self.assertEqual(302, r.status_code)
        self.assertEqual(0, Form.query.first().counter)

    @httpretty.activate    
    def test_fail_with_invalid_reply_to(self):
        httpretty.register_uri(httpretty.POST, 'https://api.sendgrid.com/api/mail.send.json')

        # manually confirm
        r = self.client.post('/carlitos@example.com',
            headers = {'Referer': 'http://carlitos.net/'},
            data={'name': 'carlitos'}
        )
        f = Form.query.first()
        f.confirm_sent = True
        f.confirmed = True
        DB.session.add(f)
        DB.session.commit()

        # fail with an invalid '_replyto'
        httpretty.reset()
        r = self.client.post('/carlitos@example.com',
            headers = {'Referer': 'http://carlitos.net/'},
            data={'name': 'Real Stock', '_replyto': 'The best offers.'}
        )
        self.assertEqual(False, httpretty.has_request())
        self.assertEqual(400, r.status_code)
        self.assertEqual(0, Form.query.first().counter)

        # fail with an invalid 'email'
        httpretty.reset()
        r = self.client.post('/carlitos@example.com',
            headers = {'Referer': 'http://carlitos.net/'},
            data={'name': 'Real Stock', 'email': 'The best offers.'}
        )
        self.assertEqual(False, httpretty.has_request())
        self.assertEqual(400, r.status_code)
        self.assertEqual(0, Form.query.first().counter)

    @httpretty.activate    
    def test_activation_workflow(self):
        httpretty.register_uri(httpretty.POST, 'https://api.sendgrid.com/api/mail.send.json')
        r = self.client.post('/bob@example.com',
            headers = ajax_headers,
            data={'name': 'bob'}
        )
        f = Form.query.first()
        self.assertEqual(f.email, 'bob@example.com')
        self.assertEqual(f.host, 'example.com')
        self.assertEqual(f.confirm_sent, True)
        self.assertEqual(f.counter, 0) # the counter shows zero submissions
        self.assertEqual(f.owner_id, None)
        self.assertEqual(f.get_monthly_counter(), 0) # monthly submissions also 0

        # form has another submission, number of forms in the table should increase?
        r = self.client.post('/bob@example.com',
            headers = ajax_headers,
            data={'name': 'bob'}
        )
        number_of_forms = Form.query.count()
        self.assertEqual(number_of_forms, 1) # still only one form

        # assert form data is still the same
        f = Form.query.first()
        self.assertEqual(f.email, 'bob@example.com')
        self.assertEqual(f.host, 'example.com')
        self.assertEqual(f.confirm_sent, True)
        self.assertEqual(f.counter, 0) # still zero submissions
        self.assertEqual(f.owner_id, None)

        # test clicking of activation link
        self.client.get('/confirm/%s' % (f.hash,))

        f = Form.query.first()
        self.assertEqual(f.confirmed, True)

        # a third submission should now increase the counter
        r = self.client.post('/bob@example.com',
            headers = ajax_headers,
            data={'name': 'bob'}
        )
        number_of_forms = Form.query.count()
        self.assertEqual(number_of_forms, 1) # still only one form

        f = Form.query.first()
        self.assertEqual(f.email, 'bob@example.com')
        self.assertEqual(f.host, 'example.com')
        self.assertEqual(f.confirm_sent, True)
        self.assertEqual(f.owner_id, None)
        self.assertEqual(f.counter, 1) # counter has increased
        self.assertEqual(f.get_monthly_counter(), 1) # monthly submissions also

    @httpretty.activate
    def test_monthly_limits(self):
        httpretty.register_uri(httpretty.POST, 'https://api.sendgrid.com/api/mail.send.json')

        # monthly limit is set to 2 during tests
        self.assertEqual(settings.MONTHLY_SUBMISSIONS_LIMIT, 2)

        # manually verify luke@example.com
        r = self.client.post('/luke@example.com',
            headers = ajax_headers,
            data={'name': 'luke'}
        )
        f = Form.query.first()
        f.confirm_sent = True
        f.confirmed = True
        DB.session.add(f)
        DB.session.commit()

        # first submission
        httpretty.register_uri(httpretty.POST, 'https://api.sendgrid.com/api/mail.send.json')
        r = self.client.post('/luke@example.com',
            headers = ajax_headers,
            data={'name': 'peter'}
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn('peter', httpretty.last_request().body)

        # second submission
        httpretty.register_uri(httpretty.POST, 'https://api.sendgrid.com/api/mail.send.json')
        r = self.client.post('/luke@example.com',
            headers = ajax_headers,
            data={'name': 'ana'}
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn('ana', httpretty.last_request().body)

        # third submission, now we're over the limit
        httpretty.register_uri(httpretty.POST, 'https://api.sendgrid.com/api/mail.send.json')
        r = self.client.post('/luke@example.com',
            headers = ajax_headers,
            data={'name': 'maria'}
        )
        self.assertEqual(r.status_code, 200) # the response to the user is the same
                                             # being the form over the limits or not

        # but the mocked sendgrid should never receive this last form
        self.assertNotIn('maria', httpretty.last_request().body)
        self.assertIn('You+are+past+our+limit', httpretty.last_request().body)

        # all the other variables are ok:
        self.assertEqual(1, Form.query.count())
        f = Form.query.first()
        self.assertEqual(f.counter, 3)
        self.assertEqual(f.get_monthly_counter(), 3) # the counters mark 4

        # the user pays and becomes upgraded
        r = self.client.post('/register',
            data={'email': 'luke@example.com',
                  'password': 'banana'}
        )
        user = User.query.filter_by(email='luke@example.com').first()
        user.upgraded = True
        user.emails = [Email(address='luke@example.com')]
        DB.session.add(user)
        DB.session.commit()

        # the user should receive form posts again
        httpretty.register_uri(httpretty.POST, 'https://api.sendgrid.com/api/mail.send.json')
        r = self.client.post('/luke@example.com',
            headers = ajax_headers,
            data={'name': 'noah'}
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn('noah', httpretty.last_request().body)
