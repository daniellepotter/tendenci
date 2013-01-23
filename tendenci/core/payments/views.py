from django.conf import settings
from django.shortcuts import render_to_response, get_object_or_404
from django.template import RequestContext
from django.http import HttpResponseRedirect
from django.core.urlresolvers import reverse

from tendenci.core.payments.models import Payment
from tendenci.core.payments.authorizenet.utils import prepare_authorizenet_sim_form
from tendenci.core.payments.forms import PaymentGatewayForm
from tendenci.apps.invoices.models import Invoice
from tendenci.core.base.http import Http403
from tendenci.core.base.utils import tcurrency
from tendenci.core.event_logs.models import EventLog
from tendenci.core.site_settings.utils import get_setting

def pay_online(request, invoice_id, guid="", template_name="payments/pay_online.html"):
    # check if they have the right to view the invoice
    invoice = get_object_or_404(Invoice, pk=invoice_id)
    if not invoice.allow_view_by(request.user, guid): raise Http403

    gateway_chosen = False
    form = None
    post_url = ""

    if request.method == 'POST':
        gateway_form = PaymentGatewayForm(request.POST)
        if gateway_form.is_valid():
            merchant_account = gateway_form.cleaned_data['gateway']
            gateway_chosen = True
            # tender the invoice
            if not invoice.is_tendered:
                invoice.tender(request.user)
                # log an event for invoice edit
                EventLog.objects.log(instance=invoice)  
              
            # generate the payment
            payment = Payment(merchant_account=merchant_account)
            
            boo = payment.payments_pop_by_invoice_user(request.user, invoice, guid)
            # log an event for payment add
            EventLog.objects.log(instance=payment)
            
            # post payment form to gateway and redirect to the vendor so customer can pay from there
            if boo:
                #merchant_account = (get_setting("site", "global", "merchantaccount")).lower()
                
                if merchant_account.name == 'stripe':
                    return HttpResponseRedirect(reverse('stripe.payonline', args=[payment.id]))    
                else:

                    if merchant_account.name == "authorizenet":
                        form = prepare_authorizenet_sim_form(request, payment)
                        post_url = merchant_account.get_value_of("AUTHNET_POST_URL")
                    elif merchant_account.name == 'firstdata':
                        from tendenci.core.payments.firstdata.utils import prepare_firstdata_form
                        form = prepare_firstdata_form(request, payment)
                        post_url = merchant_account.get_value_of("FIRSTDATA_POST_URL")
                    elif merchant_account.name == 'paypalpayflowlink':
                        from tendenci.core.payments.payflowlink.utils import prepare_payflowlink_form
                        form = prepare_payflowlink_form(request, payment)
                        post_url = merchant_account.get_value_of("PAYFLOWLINK_POST_URL")
                    elif merchant_account.name == 'paypal':
                        from tendenci.core.payments.paypal.utils import prepare_paypal_form
                        form = prepare_paypal_form(request, payment)
                        post_url = merchant_account.get_value_of("PAYPAL_POST_URL")
                    else:   # more vendors 
                        form = None
                        post_url = ""
    else:
        gateway_form = PaymentGatewayForm()
        
    return render_to_response(template_name, 
                              {'form':form, 'post_url':post_url,
                               'gateway_form':gateway_form, 'gateway_chosen':gateway_chosen}, 
                              context_instance=RequestContext(request))
    
def view(request, id, guid=None, template_name="payments/view.html"):
    payment = get_object_or_404(Payment, pk=id)

    if not payment.allow_view_by(request.user, guid): raise Http403
    #payment.amount = tcurrency(payment.amount)
    
    return render_to_response(template_name, {'payment':payment}, 
        context_instance=RequestContext(request))
    
def receipt(request, id, guid, template_name='payments/receipt.html'):
    payment = get_object_or_404(Payment, pk=id)
    if payment.guid <> guid:
        raise Http403
        
    return render_to_response(template_name,{'payment':payment},
                              context_instance=RequestContext(request))

        


