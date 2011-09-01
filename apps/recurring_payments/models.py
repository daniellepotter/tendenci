import uuid
from datetime import datetime
from decimal import Decimal
from django.db import models
from django.contrib.auth.models import User
from django.utils.translation import ugettext_lazy as _
from django.contrib.contenttypes.models import ContentType
from django.db.models import Sum
from dateutil.relativedelta import relativedelta
from invoices.models import Invoice
from profiles.models import Profile
from recurring_payments.authnet.cim import (CIMCustomerProfile,
                                            CIMCustomerProfileTransaction)
from recurring_payments.authnet.utils import payment_update_from_response
from payments.models import Payment

BILLING_PERIOD_CHOICES = (
                        ('month', _('Month(s)')),
                        ('year', _('Year(s)')),
                        ('week', _('Week(s)')),
                        ('day', _('Day(s)')),
                        )


class RecurringPayment(models.Model):
    guid = models.CharField(max_length=50)
    # gateway assigned ID associated with the customer profile
    customer_profile_id = models.CharField(max_length=100, default='')
    user = models.ForeignKey(User, related_name="recurring_payment_user",
                             verbose_name=_('Customer'),  null=True)
    description = models.CharField(max_length=100)
    # with object_content_type and object_content_id, we can apply the recurring 
    # payment to other modules such as memberships, jobs, etc.
    object_content_type = models.ForeignKey(ContentType, blank=True, null=True)
    object_content_id = models.IntegerField(default=0, blank=True, null=True)

    billing_period = models.CharField(max_length=50, choices=BILLING_PERIOD_CHOICES,
                                        default='month')
    billing_frequency = models.IntegerField(default=1)
    billing_start_dt = models.DateTimeField(_("Initial billing cycle start date"), 
                                            help_text=_("The initial start date for the recurring payments."+\
                                            "That is, the start date of the first billing cycle."))
    # num days after billing cycle end date to determine billing_dt or payment due date
    num_days = models.IntegerField(default=0)
#    due_borf = models.CharField(_("Before or after"), max_length=20,
#                                   choices=DUE_BORF_CHOICES, default='before') 
#    due_sore = models.CharField(_("Billing cycle start or end date"), max_length=20,
#                                   choices=DUE_SORE_CHOICES, default='start')
    
    #billing_cycle_start_dt = models.DateTimeField(_("Billing cycle start date"))
    #billing_cycle_end_dt = models.DateTimeField(_('Billing cycle end date'), blank=True, null=True)
    payment_amount = models.DecimalField(max_digits=15, decimal_places=2)
    has_trial_period = models.BooleanField(default=0)
    trial_period_start_dt = models.DateTimeField(_('Trial period start date'), blank=True, null=True)
    trial_period_end_dt = models.DateTimeField(_('Trial period end date'), blank=True, null=True)
    trial_amount = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True)

    next_billing_dt = models.DateTimeField(blank=True, null=True)
    last_payment_received_dt = models.DateTimeField(blank=True, null=True)
    num_billing_cycle_completed = models.IntegerField(default=0, blank=True, null=True)
    num_billing_cycle_failed = models.IntegerField(default=0, blank=True, null=True)
    
    current_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    outstanding_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    

    create_dt = models.DateTimeField(auto_now_add=True)
    update_dt = models.DateTimeField(auto_now=True)
    creator = models.ForeignKey(User, related_name="recurring_payment_creator",  null=True)
    creator_username = models.CharField(max_length=50, null=True)
    owner = models.ForeignKey(User, related_name="recurring_payment_owner", null=True)
    owner_username = models.CharField(max_length=50, null=True)
    status_detail = models.CharField(max_length=50, default='active')
    status = models.BooleanField(default=True)
    
    
    def __unicode__(self):
        return '%s - %s' % (self.user, self.description)
    
    def save(self, *args, **kwargs):
        if not self.id:
            self.guid = str(uuid.uuid1())
        super(RecurringPayment, self).save(*args, **kwargs)
        
    def populate_payment_profile(self, *args, **kwargs):
        """
        Check payment gateway for the payment profiles for this customer 
        and store the payment profile info locally
        """
        customer_profile = CIMCustomerProfile(self.customer_profile_id)
        success, response_d = customer_profile.get()
        
        if success:
            if response_d['profile'].has_key('payment_profiles'):
                cim_payment_profiles = response_d['profile']['payment_profiles']
                if not type(cim_payment_profiles) is list:
                    cim_payment_profiles = list([cim_payment_profiles])
                for cim_payment_profile in cim_payment_profiles:
                    customer_payment_profile_id = cim_payment_profile['customer_payment_profile_id']
                    
                    # check if already exists, if not, insert to payment profiles table
                    payment_profile_exists = PaymentProfile.objects.filter(
                                                    payment_profile_id=customer_payment_profile_id
                                                    ).exists()
                    
                    if not payment_profile_exists:
                    
                        payment_profile = PaymentProfile(recurring_payment=self,
                                                         payment_profile_id=customer_payment_profile_id,
                                                         creator=self.user,
                                                         owner=self.user,
                                                         creator_username= self.user.username,
                                                         owner_username = self.user.username,
                                                         )
                        if cim_payment_profile['payment'].has_key('credit_card') and \
                                    cim_payment_profile['payment']['credit_card'].has_key('card_number'):
    
                            card_num = cim_payment_profile['payment']['credit_card']['card_number'][-4:]
    
                            payment_profile.card_num = card_num
                        payment_profile.save()
        else: # failed
            # info admin that an error occurred when populating payment profiles
            pass   
        
        
        
        
    def within_trial_period(self):
        now = datetime.now()
        
        # billing period has already started, skip the trial period.
        if now >= self.billing_start_dt:
            return False
        
        if all([self.has_trial_period, self.trial_period_start_dt]):
            if not self.trial_period_end_dt or self.trial_period_end_dt > self.billing_start_dt:
                self.trial_period_end_dt = self.billing_start_dt
            
            return now <= self.trial_period_end_dt
        
        return False
    
    def get_next_billing_cycle(self, last_billing_cycle=None):
        now = datetime.now()
        if self.billing_period == 'year':
            timedelta = relativedelta(years=self.billing_frequency)
        elif self.billing_period == 'month':
            timedelta = relativedelta(months=self.billing_frequency)
        elif self.billing_period == 'day':
            timedelta = relativedelta(days=self.billing_frequency)
        elif self.billing_period == 'week':
            timedelta = relativedelta(days=self.billing_frequency*7)
        else:
            timedelta = relativedelta(months=self.billing_frequency)
        
        # the first billing            
        if not last_billing_cycle:
            if self.within_trial_period():
                if not self.trial_period_end_dt or self.trial_period_end_dt > self.billing_start_dt:
                    self.trial_period_end_dt = self.billing_start_dt
                return (self.trial_period_start_dt, self.trial_period_end_dt)
            else:
                billing_cycle_start = self.billing_start_dt
                billing_cycle_end = billing_cycle_start + timedelta
        else:
            billing_cycle_start = last_billing_cycle['end'] + relativedelta(days=1)

            billing_cycle_end = billing_cycle_start + timedelta
            
        
        return (billing_cycle_start, billing_cycle_end)  
    
    def get_last_billing_cycle(self):
        rp_invs = RecurringPaymentInvoice.objects.filter(
                            recurring_payment=self).order_by('-billing_cycle_start_dt')
        if rp_invs and self.billing_start_dt <= rp_invs[0].billing_cycle_start_dt:
            return (rp_invs[0].billing_cycle_start_dt, rp_invs[0].billing_cycle_end_dt)
        else:
            return None
        
    def billing_cycle_t2d(self, billing_cycle):
        # convert tuple to dict
        if billing_cycle:
            billing_cycle = dict(zip(('start', 'end'), billing_cycle))
        return billing_cycle
    
    def get_payment_due_date(self, billing_cycle):
        """
        Get the payment due date for the billing cycle.
        """
        # num_days is the number days after billing cycle end date
        billing_dt = billing_cycle['end'] + relativedelta(days=self.num_days)
        
        return billing_dt
        
        
    def check_and_generate_invoices(self, last_billing_cycle=None):
        """
        Check and generate invoices if needed.
        """
        now = datetime.now()
        if not last_billing_cycle:
            last_billing_cycle = self.billing_cycle_t2d(self.get_last_billing_cycle())
            
        next_billing_cycle = self.billing_cycle_t2d(self.get_next_billing_cycle(last_billing_cycle))
        billing_dt = self.get_payment_due_date(next_billing_cycle)
        
        # determine when to create the invoice - 
        # on the billing cycle end date
        invoice_create_dt = next_billing_cycle['end']
        
        if invoice_create_dt <= now:
            self.create_invoice(next_billing_cycle, billing_dt)
            # might need to notify admin and/or user that an invoice has been created.
            
            self.check_and_generate_invoices(next_billing_cycle)
           
            
        
    def create_invoice(self, billing_cycle, billing_dt):
        """
        Create an invoice and update the next_billing_dt for this recurring payment.
        """
        try:
            profile = self.user.get_profile()
        except Profile.DoesNotExist:
            profile = Profile.objects.create_profile(user=self.user)
        
        if self.within_trial_period():
            amount = self.trial_amount
        else:
            amount = self.payment_amount
            
        self.next_billing_dt = billing_dt
        
        self.save()
            
        inv = Invoice()
        inv.due_date = billing_dt
        inv.ship_date = billing_dt

        inv.object_type = ContentType.objects.get(app_label=self._meta.app_label, 
                                                  model=self._meta.module_name)
        inv.object_id = self.id
        inv.title = "Recurring Payment Invoice"
        inv.bill_to = self.user.get_full_name()
        inv.bill_to_company = profile.company
        inv.bill_to_address = profile.address
        inv.bill_to_city = profile.city
        inv.bill_to_state = profile.state
        inv.bill_to_zip_code = profile.zipcode
        inv.bill_to_country = profile.country
        inv.bill_to_phone = profile.phone
        inv.bill_to_email = profile.email
        inv.status = True
        
        inv.total = amount
        inv.subtotal = inv.total
        inv.balance = inv.total
        inv.estimate = 1
        inv.status_detail = 'estimate'
        inv.save(self.user)
        
        rp_invoice = RecurringPaymentInvoice(
                             recurring_payment=self,
                             invoice=inv,
                             billing_cycle_start_dt=billing_cycle['start'],
                             billing_cycle_end_dt=billing_cycle['end'],
                             billing_dt=billing_dt

                                             )
        rp_invoice.save()
        
        return rp_invoice
    
    def get_current_balance(self):
        d = RecurringPaymentInvoice.objects.filter(
                                invoice__balance__gt=0,
                                billing_cycle_start_dt__lte=datetime.now(),
                                billing_cycle_end_dt__gte=datetime.now()
                                ).aggregate(current_balance=Sum('invoice__balance'))
        if not d['current_balance']:
            d['current_balance'] = 0
        return d['current_balance']
    
    def get_outstanding_balance(self):
        d = RecurringPaymentInvoice.objects.filter(
                                invoice__balance__gt=0,
                                billing_dt__lte=datetime.now()
                                ).aggregate(outstanding_balance=Sum('invoice__balance'))
        if not d['outstanding_balance']:
            d['outstanding_balance'] = 0
        return d['outstanding_balance']
        
class PaymentProfile(models.Model):
    recurring_payment =  models.ForeignKey(RecurringPayment)
    # assigned by gateway
    payment_profile_id = models.CharField(max_length=100, unique=True)
    card_type = models.CharField(max_length=50, null=True)
    # last 4 digits of card number
    card_num = models.CharField(max_length=4, null=True)
    expiration_dt = models.DateTimeField(blank=True, null=True)

    create_dt = models.DateTimeField(auto_now_add=True)
    update_dt = models.DateTimeField(auto_now=True)
    creator = models.ForeignKey(User, related_name="payment_profile_creator",  null=True)
    creator_username = models.CharField(max_length=50, null=True)
    owner = models.ForeignKey(User, related_name="payment_profile_owner", null=True)
    owner_username = models.CharField(max_length=50, null=True)
    status_detail = models.CharField(max_length=50, default='active')
    status = models.BooleanField(default=True)   
        
class RecurringPaymentInvoice(models.Model):
    recurring_payment =  models.ForeignKey(RecurringPayment, related_name="rp_invoices")
    invoice = models.ForeignKey(Invoice, blank=True, null=True)
    billing_cycle_start_dt = models.DateTimeField(_("Billing cycle start date"), blank=True, null=True)
    billing_cycle_end_dt = models.DateTimeField(_('Billing cycle end date'), blank=True, null=True)
    # billing date is same as due date in invoice
    billing_dt = models.DateTimeField(blank=True, null=True)
    payment_received_dt = models.DateTimeField(blank=True, null=True)
    create_dt = models.DateTimeField(auto_now_add=True)
    #is_paid = models.BooleanField(default=False)
    
    def make_payment_transaction(self, payment_profile_id):
        """
        Make a payment transaction. This includes:
        1) Make an API call createCustomerProfileTransactionRequest
        2) Create a payment transaction entry
        3) Create a payment entry
        4) If the transaction is successful, populate payment entry with the direct response and mark payment as paid
        """
        amount = self.invoice.balance
        # tender the invoice
        self.invoice.tender(self.recurring_payment.user)
        
        d = {'amount': amount,
             }
        
        cpt = CIMCustomerProfileTransaction(self.recurring_payment.customer_profile_id,
                                            payment_profile_id)
        
        success, response_d = cpt.create(**d)
       
        # create a payment transaction record 
        payment_transaction = PaymentTransaction(
                                    recurring_payment = self.recurring_payment,
                                    recurring_payment_invoice = self,
                                    payment_profile_id = payment_profile_id,
                                    trans_type='auth_capture',
                                    amount=amount,
                                    status=success)
        
        # create a payment record
        payment = Payment()

        payment.payments_pop_by_invoice_user(self.recurring_payment.user, 
                                             self.invoice, 
                                             self.invoice.guid)
        # update the payment entry with the direct response returned from payment gateway
        payment = payment_update_from_response(payment, response_d['direct_response'])

        if success:
            payment.mark_as_paid()
            payment.save()
            self.invoice.make_payment(self.recurring_payment.user, Decimal(payment.amount))
            self.invoice.save()
            
            self.payment_received_dt = datetime.now()
        else:
            if payment.status_detail == '':
                payment.status_detail = 'not approved'
                payment.save()
            
        payment_transaction.payment = payment
        payment_transaction.result_code = response_d['result_code']
        payment_transaction.message_code = response_d['message_code']
        payment_transaction.message_text = response_d['message_text']
            
        payment_transaction.save()
        
        return payment_transaction


class PaymentTransaction(models.Model):
    recurring_payment =  models.ForeignKey(RecurringPayment, related_name="transactions")
    recurring_payment_invoice =  models.ForeignKey(RecurringPaymentInvoice, related_name="transactions")
    payment_profile_id = models.CharField(max_length=100, default='')
    # trans_type - capture, refund or void
    trans_type  = models.CharField(max_length=50, null=True) 
    # refid       
    payment =  models.ForeignKey(Payment, null=True)       
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    
    result_code = models.CharField(max_length=10, default='')
    message_code = models.CharField(max_length=20, default='')
    message_text = models.CharField(max_length=200, default='')
    
    create_dt = models.DateTimeField(auto_now_add=True)
    creator = models.ForeignKey(User, related_name="payment_transaction_creator",  null=True)
    # True=success or False=failed
    status = models.BooleanField()
    #status_detail = models.CharField(max_length=50,  null=True)