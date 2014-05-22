import jinja2
import json
import logging
import urlparse
import webapp2
import urllib

from google.appengine.api import urlfetch
from google.appengine.api import mail
from google.appengine.api import memcache
from google.appengine.ext import db
from google.appengine.ext import deferred

import model
import stripe
import wp_import

# These get added to every pledge calculation
PRE_SHARDING_TOTAL = 27425754  # See model.ShardedCounter
WP_PLEDGE_TOTAL = 41326868
DEMOCRACY_DOT_COM_BALANCE = 8553428
CHECKS_BALANCE = 7655200  # lol US government humor


class Error(Exception): pass

JINJA_ENVIRONMENT = jinja2.Environment(
  loader=jinja2.FileSystemLoader('templates/'),
  extensions=['jinja2.ext.autoescape'],
  autoescape=True)


def send_thank_you(name, email, url_nonce, amount_cents):
  """Deferred email task"""
  sender = ('MayOne no-reply <noreply@%s.appspotmail.com>' %
            model.Config.get().app_name)
  subject = 'Thank you for your pledge'
  message = mail.EmailMessage(sender=sender, subject=subject)
  message.to = email

  format_kwargs = {
    # TODO: Figure out how to set the outgoing email content encoding.
    #  once we can set the email content encoding to utf8, we can change this
    #  to name.encode('utf-8') and not drop fancy characters. :(
    'name': name.encode('ascii', errors='ignore'),
    'url_nonce': url_nonce,
    'total': '$%d' % int(amount_cents/100)
  }

  message.body = open('email/thank-you.txt').read().format(**format_kwargs)
  message.html = open('email/thank-you.html').read().format(**format_kwargs)
  message.send()

# Respond to /OPTION requests in a way that allows cross site requests
# TODO(hjfreyer): Pull into some kind of middleware?
def enable_cors(handler):
  if 'Origin' in handler.request.headers:
    origin = handler.request.headers['Origin']
    _, netloc, _, _, _, _ = urlparse.urlparse(origin)
    if not (netloc == 'mayone.us' or netloc.endswith('.mayone.us')):
      logging.warning('Invalid origin: ' + origin)
      handler.error(403)
      return

    handler.response.headers.add_header("Access-Control-Allow-Origin", origin)
    handler.response.headers.add_header("Access-Control-Allow-Methods", "POST")
    handler.response.headers.add_header("Access-Control-Allow-Headers", "content-type, origin")

# TODO(hjfreyer): Tests!!
class ContactHandler(webapp2.RequestHandler):
  def post(self):
    data = json.loads(self.request.body)
    ascii_name = data["name"].encode('ascii', errors='ignore')
    ascii_email = data["email"].encode('ascii', errors='ignore')
    ascii_subject = data["subject"].encode('ascii', errors='ignore')
    ascii_body = data["body"].encode('ascii', errors='ignore')


    message = mail.EmailMessage(sender=('MayOne no-reply <noreply@%s.appspotmail.com>' %
                                        model.Config.get().app_name),
                                subject=ascii_subject)
    message.to = "info@mayone.us"
    message.body = 'FROM: %s\n\n%s' % (ascii_email, ascii_body)
    message.send()
    enable_cors(self)
    self.response.write('Ok.')

  def options(self):
    enable_cors(self)


class GetTotalHandler(webapp2.RequestHandler):
  def get(self):
    total = (PRE_SHARDING_TOTAL +
             WP_PLEDGE_TOTAL +
             DEMOCRACY_DOT_COM_BALANCE +
             CHECKS_BALANCE)
    total += model.ShardedCounter.get_count('TOTAL')
    total = int(total/100) * 100
    self.response.headers['Content-Type'] = 'application/javascript'
    self.response.write('%s(%d)' % (self.request.get('callback'), total))


class GetStripePublicKeyHandler(webapp2.RequestHandler):
  def get(self):
    if not model.Config.get().stripe_public_key:
      raise Error('No public key in DB')
    self.response.write(model.Config.get().stripe_public_key)


class EmbedHandler(webapp2.RequestHandler):
  def get(self):
    if self.request.get('widget') == '1':
      self.redirect('/embed.html')
    else:
      self.redirect('/')


class PledgeHandler(webapp2.RequestHandler):
  def post(self):
    try:
      data = json.loads(self.request.body)
    except:
      logging.warning('Bad JSON request')
      self.error(400)
      self.response.write('Invalid request')
      return

    # ugh, consider using validictory?
    if ('email' not in data or
        'token' not in data or
        'amount' not in data or
        'userinfo' not in data or
        'occupation' not in data['userinfo'] or
        'employer' not in data['userinfo'] or
        'phone' not in data['userinfo'] or
        'target' not in data['userinfo']):
      self.error(400)
      self.response.write('Invalid request')
      return
    email = data['email']
    token = data['token']
    amount = data['amount']
    name = data.get('name', '')

    occupation = data['userinfo']['occupation']
    employer = data['userinfo']['employer']
    phone = data['userinfo']['phone']
    target = data['userinfo']['target']

    try:
      amount = int(amount)
    except ValueError:
      self.error(400)
      self.response.write('Invalid request')
      return

    if not (email and token and amount and occupation and employer and target):
      self.error(400)
      self.response.write('Invalid request: missing field')
      return

    if not mail.is_email_valid(email):
      self.error(400)
      self.response.write('Invalid request: Bad email address')
      return

    stripe.api_key = model.Config.get().stripe_private_key
    customer = stripe.Customer.create(card=token, email=email)

    pledge = model.addPledge(
            email=email, amount_cents=amount, stripe_customer_id=customer.id,
            occupation=occupation, employer=employer, phone=phone,
            target=target, note=self.request.get('note'))

    # Add thank you email to a task queue
    deferred.defer(send_thank_you, name or email, email,
                   pledge.url_nonce, amount, _queue='mail')

    # Add to the total asynchronously.
    deferred.defer(model.increment_donation_total, amount,
                   _queue='incrementTotal')

    self.response.write('Ok.')


# Paypal Step 1:  We tell Paypal *what* we want the user to do
class PaypalStartHandler(webapp2.RequestHandler):
  def post(self):
    try:
      data = json.loads(self.request.body)
    except:
      self.error(400)
      self.response.write('Invalid request')
      return

    # ugh, consider using validictory?
    if ('amount' not in data or
        'userinfo' not in data or
        'occupation' not in data['userinfo'] or
        'employer' not in data['userinfo'] or
        'phone' not in data['userinfo'] or
        'target' not in data['userinfo']):
      logging.warning("Paypal request invalid")
      self.error(400)
      self.response.write('Invalid request')
      return

    amount = data['amount']

    config = model.Config.get()

    form_fields = {
      "VERSION": "113",
      "USER": config.paypal_user,
      "PWD": config.paypal_password,
      "SIGNATURE": config.paypal_signature,
      "METHOD": "SetExpressCheckout",
      "RETURNURL": self.request.host_url + '/paypal-return',
      "CANCELURL": self.request.host_url + '/pledge',
      "NOSHIPPING": "1" if amount < 50 else "0",
      "REQCONFIRMSHIPPING": "0" if amount < 50 else "1",
      "PAYMENTREQUEST_0_AMT": amount,
      "PAYMENTREQUEST_0_PAYMENTACTION": "Authorization",
      "PAYMENTREQUEST_0_CUSTOM": urllib.urlencode(data['userinfo']),
      "PAYMENTREQUEST_0_NAME": "Pledge to MayDay One PAC",
      "PAYMENTREQUEST_0_AMT":  amount,
      "PAYMENTREQUEST_0_QTY":  "1",
      "L_PAYMENTREQUEST_0_NAME0": "Pledge to MayDay One PAC",
      "L_PAYMENTREQUEST_0_AMT0":  amount,
      "L_PAYMENTREQUEST_0_QTY0":  "1",
      "SOLUTIONTYPE":  "Sole",
      "BRANDNAME":  "MayDay PAC",
      # TODO FIXME - LOGOIMG trumps if given; it's a different look with HDRIMG
      "LOGOIMG":  self.request.host_url + '/static/paypal_logoimg.png',
      #"HDRIMG":   self.request.host_url + '/static/paypal_hdrimg.png',
      #"PAYFLOWCOLOR":    "00FF00",
      #"CARTBORDERCOLOR": "0000FF",
      # TODO FIXME Explore colors.  Seems like either color has same result, and cart trumps
    }
    form_data = urllib.urlencode(form_fields)

    result = urlfetch.fetch(url=config.paypal_api_url, payload=form_data, method=urlfetch.POST,
                headers={'Content-Type': 'application/x-www-form-urlencoded'})

    result_map = urlparse.parse_qs(result.content)

    try:
      if result_map['ACK'][0] != 'Success':
        raise NameError('Paypal failed to give Success ACK')
      if result_map['TOKEN'][0] is None:
        raise NameError('Paypal failed to return a TOKEN')

    except Exception, e:
      self.error(400)
      self.response.write('Paypal SetExpressCheckout failed.')
      self.response.write(str(e))
      logging.warning(str(e) + result.content[:1000])
      return

    self.response.headers['Content-Type'] = 'application/json'
    self.response.write('{"redirect":"' + config.paypal_url + "?cmd=_express-checkout&token="
                            + result_map['TOKEN'][0] + '"}')

    #  And now the user is supposed to go off and do it...

# Paypal Step 2: Paypal returns to us, telling us the user has agreed to do it
#                So we get the details so the user can confirm them
class PaypalDetailsHandler(webapp2.RequestHandler):
  def post(self):
    try:
      data = json.loads(self.request.body)
    except:
      logging.warning("Bad JSON request")
      self.error(400)
      self.response.write('Invalid request')
      return

    token = data['token']

    if not token:
      logging.warning("Paypal completion missing token: " + self.request.url)
      self.error(400);
      self.response.write("Unusual error: no token from Paypal.  Please contact info@mayone.us and report these details:")
      self.response.write(self.request.url)
      return

    config = model.Config.get()

    # Fetch the details of this pending transaction
    form_fields = {
      "VERSION": "113",
      "USER": config.paypal_user,
      "PWD": config.paypal_password,
      "SIGNATURE": config.paypal_signature,
      "METHOD": "GetExpressCheckoutDetails",
      "TOKEN": token
    }
    form_data = urllib.urlencode(form_fields)

    result = urlfetch.fetch(url=config.paypal_api_url, payload=form_data, method=urlfetch.POST,
                headers={'Content-Type': 'application/x-www-form-urlencoded'})
    result_map = urlparse.parse_qs(result.content)
    json.dump(result_map, self.response)


# Paypal Step 3: The user has affirmed the pledge - book it!
class PaypalCompleteHandler(webapp2.RequestHandler):
  def post(self):
    token = self.request.get("token")
    payer_id = self.request.get("payer_id")
    amount = self.request.get("amount")
    cents = int(float(amount)) * 100
    occupation = self.request.get("occupation")
    employer = self.request.get("employer")
    phone = self.request.get("phone")
    email = self.request.get("email")
    note = self.request.get("note")
    name = self.request.get("name")
    target = self.request.get("target")

    if not token:
      logging.warning("Paypal completion missing token: " + self.request.url)
      self.error(400);
      self.response.write("Unusual error: no token from Paypal.  Please contact info@mayone.us and report these details:")
      self.response.write(self.request.url)
      return

    config = model.Config.get()

    # Now we have to DoExpressCheckoutPayment and *then* we are done...
    form_fields = {
      "VERSION": "113",
      "USER": config.paypal_user,
      "PWD": config.paypal_password,
      "SIGNATURE": config.paypal_signature,
      "METHOD": "DoExpressCheckoutPayment",
      "PAYMENTREQUEST_0_PAYMENTACTION": "Authorization",
      "TOKEN": token,
      "PAYERID": payer_id,
      "PAYMENTREQUEST_0_AMT": amount,
      "PAYMENTREQUEST_0_NAME": "Pledge to MayDay One PAC",
      "PAYMENTREQUEST_0_QTY":  "1",
    }

    form_data = urllib.urlencode(form_fields)

    result = urlfetch.fetch(url=config.paypal_api_url, payload=form_data, method=urlfetch.POST,
                headers={'Content-Type': 'application/x-www-form-urlencoded'})

    result_map = urlparse.parse_qs(result.content)
    if result_map['ACK'][0] != 'Success':
      logging.warning("Paypal DoExpressCheckout failed. ")
      logging.warning("Response: " + result.content)

      self.error(400);
      self.response.write("Unusual error: No Checkout Completion from Paypal.  Please contact info@mayone.us and report these details:")
      self.response.write(result.content)
      return

    #  At this point, we have finished the whole Paypal cycle; we just
    #   need to record all our information

    pledge = model.addPledge(
            email=email, amount_cents=cents, paypal_txn_id=result_map['PAYMENTINFO_0_TRANSACTIONID'][0],
            paypal_token=token, paypal_payer_id=payer_id,
            occupation=occupation, employer=employer, phone=phone,
            target=target, note=note)

    # Add thank you email to a task queue
    deferred.defer(send_thank_you, name or email, email,
                   pledge.url_nonce, cents, _queue="mail")

    # Add to the total asynchronously.
    deferred.defer(model.increment_donation_total, cents, _queue="incrementTotal")

    self.redirect("/thankyou")
    # Add thank you email to a task queue

class UserUpdateHandler(webapp2.RequestHandler):
  def get(self, url_nonce):
    user = model.User.all().filter('url_nonce =', url_nonce).get()
    if user is None:
      self.error(404)
      self.response.write('This page was not found')
      return

    template = JINJA_ENVIRONMENT.get_template('user-update.html')
    self.response.write(template.render({'user': user}))

  def post(self, url_nonce):
    try:
      user = model.User.all().filter('url_nonce =', url_nonce).get()
      if user is None:
        self.error(404)
        self.response.write('This page was not found')
        return

      user.occupation = self.request.get('occupation')
      user.employer = self.request.get('employer')
      user.phone = self.request.get('phone')
      user.target = self.request.get('target')
      user.put()
      template = JINJA_ENVIRONMENT.get_template('user-update.html')
      ctx = {'user': user, 'success': True}
      self.response.write(template.render(ctx))
    except:
      self.error(400)
      self.response.write('There was a problem submitting the form')
      return


app = webapp2.WSGIApplication([
  ('/total', GetTotalHandler),
  ('/stripe_public_key', GetStripePublicKeyHandler),
  ('/pledge.do', PledgeHandler),
  ('/paypal.start', PaypalStartHandler),
  ('/paypal.details', PaypalDetailsHandler),
  ('/paypal.complete', PaypalCompleteHandler),
  ('/user-update/(\w+)', UserUpdateHandler),
  ('/campaigns/may-one/?', EmbedHandler),
  ('/contact.do', ContactHandler),
  # See wp_import
  # ('/import.do', wp_import.ImportHandler),
], debug=False)
