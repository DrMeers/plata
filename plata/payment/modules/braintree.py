"""
Payment module for Braintree integration

Needs the following settings to work correctly::

    BRAINTREE = {
        'MERCHANT_ID': 'your_merchant_id',
        'ENVIRONMENT': 'Sandbox', # Or 'Production' or Development'
        'PUBLIC_KEY': 'your_public_key',
        'PRIVATE_KEY': 'your_private_key',
        'CSE_KEY': 'your_client_side_encryption_key',
    }

"""

from __future__ import absolute_import

from decimal import Decimal
import logging

from django.conf import settings
from django.contrib import messages
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import ugettext_lazy as _
from django.views.decorators.http import require_POST

import braintree # absolute_import above to avoid importing *this* module!

import plata
from plata.payment.modules.base import ProcessorBase
from plata.shop.models import OrderPayment


logger = logging.getLogger('plata.payment.braintree')

braintree.Configuration.configure(
    getattr(braintree.Environment, settings.BRAINTREE['ENVIRONMENT']),
    settings.BRAINTREE['MERCHANT_ID'],
    settings.BRAINTREE['PUBLIC_KEY'],
    settings.BRAINTREE['PRIVATE_KEY'],
)

class PaymentProcessor(ProcessorBase):
    key = 'braintree'
    default_name = _('Braintree')

    def get_urls(self):
        from django.conf.urls import patterns, url
        return patterns(
            '',
            url(r'^payment/braintree/submit/$',
                self.submit,
                name='plata_payment_braintree_submit',
            ),
        )

    def process_order_confirmed(self, request, order):
        if not order.balance_remaining:
            return self.already_paid(order)

        return self.shop.render(
            request,
            'payment/%s_form.html' % self.key,
            self.shop.get_context(
                request, {
                    'order': order,
                    'BRAINTREE_CSE_KEY': settings.BRAINTREE['CSE_KEY'],
                }
            )
        )

    @method_decorator(require_POST)
    def submit(self, request):
        shop = plata.shop_instance()
        order = shop.order_from_request(request)

        params = {
            'amount': order.balance_remaining.quantize(Decimal('0.00')),
            'credit_card': {
                'number': request.POST.get('number', ''),
                'cvv': request.POST.get('cvv', ''),
                'expiration_month': request.POST.get('month', ''),
                'expiration_year': request.POST.get('year', '')
            },
            'options': {
                'submit_for_settlement': True
            }
        }
        logger.debug(repr(params))
        result = braintree.Transaction.sale(params)

        if result.is_success:
            logger.info('Processing order %s using Braintree' % order)

            payment = self.create_pending_payment(order)
            if plata.settings.PLATA_STOCK_TRACKING:
                StockTransaction = plata.stock_model()
                self.create_transactions(
                    order, _('payment process reservation'),
                    type=StockTransaction.PAYMENT_PROCESS_RESERVATION,
                    negative=True, payment=payment)

            payment.status = OrderPayment.PROCESSED
            payment.currency = result.transaction.currency_iso_code
            payment.amount = result.transaction.amount
            payment.data = dict(
                map(
                    lambda (k, v): (unicode(k), repr(v)),
                    vars(result.transaction).items()
                )
            )
            payment.transaction_id = result.transaction.id
            payment.payment_method = ( # not sure how reliably this is set...
                result.transaction.credit_card.get('card_type', 'Unknown')
                if result.transaction.credit_card else 'Unknown'
            )

            if result.transaction.status == 'submitted_for_settlement':
                payment.authorized = timezone.now()
                payment.status = OrderPayment.AUTHORIZED

            payment.save()
            order = order.reload()

            logger.info(
                'Successfully processed Braintree response for %s' % order)

            if payment.authorized and plata.settings.PLATA_STOCK_TRACKING:
                StockTransaction = plata.stock_model()
                self.create_transactions(
                    order, _('sale'),
                    type=StockTransaction.SALE, negative=True, payment=payment)

            if not order.balance_remaining:
                self.order_paid(order, payment=payment, request=request)

            return self.shop.redirect('plata_order_success')
        else:
            for message in result.message.strip().split('\n'):
                messages.error(request, message)
            return self.process_order_confirmed(request, order)
