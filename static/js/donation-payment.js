/* Shared payment-flow logic for every public donation form (Donate / Live
 * To Give, Festival Seva, BACE Contribution).
 *
 * Previously each of the three form templates carried its own hand-copied
 * version of this ~150-line flow (create order -> launch Razorpay checkout
 * -> verify payment -> poll as a fallback). That duplication is exactly how
 * this flow's error handling drifted: the same "an exception here dies
 * silently, the donor is left on a dead page with no explanation" bug had
 * to be found and fixed by hand in more than one copy. This is one
 * implementation, used by all three forms -- each page only supplies the
 * handful of things that actually differ (its form element's id, and what
 * label to show on the Razorpay checkout modal).
 *
 * Payment confirmation has three layers, from most to least reliable (see
 * public.py's module docstring for the full picture -- this file only
 * implements layers 2 and 3):
 *   1. Webhook -- Razorpay's server calls our server directly. The source
 *      of truth; this file has no part in it.
 *   2. Browser fast path -- Razorpay checkout's `handler` callback, below,
 *      posts to /api/verify-payment immediately after payment. Fires in
 *      most browsers, most of the time.
 *   3. Client polling -- fallback for whenever #2 doesn't fire. Doesn't
 *      confirm anything itself; just asks whether the webhook or the fast
 *      path has already recorded success.
 *
 * Usage, once per form template:
 *
 *   <script src="{{ url_for('static', filename='js/donation-payment.js') }}"></script>
 *   <script>
 *     TempleDonationPayment.init({
 *       formId: 'my-form',
 *       description: 'Festival Seva',   // shown on the Razorpay modal
 *       orgName: {{ org_name | tojson }},
 *       razorpayEnabled: {{ razorpay_enabled | tojson }},
 *       beforeSubmit: function () { ... },  // optional, e.g. donate.html's
 *                                            // first/last-name -> full_name sync
 *     });
 *   </script>
 */
window.TempleDonationPayment = (function () {
  'use strict';

  async function postJSON(url, body, csrfToken) {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
      body: JSON.stringify(body),
    });
    let data = {};
    try { data = await resp.json(); } catch (err) { /* non-JSON error page */ }
    return { ok: resp.ok, data };
  }

  function goToReceipt(donationId) {
    window.location.href = `/donate/success/${donationId}`;
  }

  function init(config) {
    const {
      formId,
      description,
      orgName,
      razorpayEnabled,
    } = config;

    const form = document.getElementById(formId);
    if (!form) return; // page doesn't have this form (e.g. campaign not configured yet)

    const payBtn = form.querySelector('#pay-btn') || document.getElementById('pay-btn');
    const statusNote = document.getElementById('status-note');
    const csrfToken = document.querySelector('meta[name="csrf-token"]').content;

    function showStatusNote(text) {
      if (!statusNote) return;
      statusNote.textContent = text;
      statusNote.style.display = '';
    }

    function hideStatusNote() {
      if (!statusNote) return;
      statusNote.style.display = 'none';
    }

    // Fallback for whenever the Razorpay `handler` callback below doesn't
    // fire (browser closed the checkout tab weirdly, JS context interrupted,
    // etc.) -- also used right after a donor dismisses the checkout modal,
    // in case the payment actually went through moments before they closed
    // it. Doesn't confirm anything on its own; just asks the server what
    // the webhook or the fast path has already recorded.
    function pollDonationStatus(donationId, { attempts, intervalMs, quiet, onGiveUp }) {
      let count = 0;
      if (!quiet) showStatusNote('Confirming your payment... this can take up to a minute. Please don\'t close this page.');
      const timer = setInterval(async () => {
        count += 1;
        if (count > attempts) {
          clearInterval(timer);
          if (onGiveUp) onGiveUp();
          return;
        }
        try {
          const resp = await fetch(`/api/donation-status/${donationId}`);
          const status = await resp.json();
          if (status.status === 'success') {
            clearInterval(timer);
            goToReceipt(donationId);
          }
        } catch (err) {
          // Transient network hiccup -- just try again on the next tick.
        }
      }, intervalMs);
    }

    function launchRazorpayCheckout(order, donorInput) {
      const options = {
        key: order.key_id,
        amount: Math.round(order.amount * 100),
        currency: 'INR',
        name: orgName,
        description: description,
        order_id: order.order_id,
        handler: async function (response) {
          // Wrapped in try/catch: this callback runs asynchronously, well
          // after the form-submit handler that launched it has already
          // returned. An unguarded exception here (a network hiccup, a
          // browser quirk, anything) would die silently with nothing shown
          // to the donor and nothing logged anywhere a person would notice
          // -- leaving them stuck on this page having already paid, with no
          // indication anything went wrong.
          try {
            const { ok } = await postJSON('/api/verify-payment', {
              donation_id: order.donation_id,
              razorpay_order_id: response.razorpay_order_id,
              razorpay_payment_id: response.razorpay_payment_id,
              razorpay_signature: response.razorpay_signature,
            }, csrfToken);
            if (ok) {
              goToReceipt(order.donation_id);
            } else {
              pollDonationStatus(order.donation_id, {
                attempts: 40, intervalMs: 3000, quiet: false,
                onGiveUp: () => showStatusNote(
                  'Still waiting on confirmation from the payment gateway. If money was deducted, your ' +
                  'receipt will appear in "My Donations" shortly, or contact the temple office.'
                ),
              });
            }
          } catch (err) {
            console.error('Payment verification failed:', err);
            showStatusNote(
              'We could not confirm your payment automatically. If money was deducted, please note the ' +
              'time and amount and contact the temple office, or check "My Donations" shortly -- your ' +
              'receipt may still appear there once confirmation catches up.'
            );
          }
        },
        modal: {
          ondismiss: function () {
            payBtn.disabled = false;
            pollDonationStatus(order.donation_id, { attempts: 5, intervalMs: 3000, quiet: true });
          },
        },
        prefill: {
          name: donorInput.full_name,
          email: donorInput.email,
          contact: donorInput.phone,
        },
        theme: { color: '#1d3b6d' },
      };
      const rzp = new Razorpay(options);
      rzp.on('payment.failed', function () { payBtn.disabled = false; });
      rzp.open();
    }

    // The whole submit handler is wrapped in try/catch: any failure in here
    // (a network failure creating the order, the Razorpay checkout script
    // not finishing loading in time -- both seen intermittently in Safari,
    // or a server-side error) is now visible (console + on-page message)
    // and payBtn always gets a chance to re-enable, so the donor can retry
    // instead of being stuck on a dead button with no explanation.
    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      if (typeof config.beforeSubmit === 'function') config.beforeSubmit();
      payBtn.disabled = true;
      hideStatusNote();

      try {
        const donorInput = Object.fromEntries(new FormData(form).entries());
        const { ok, data: order } = await postJSON('/api/create-order', donorInput, csrfToken);

        if (!ok) {
          alert(order.error || 'Something went wrong');
          payBtn.disabled = false;
          return;
        }

        if (razorpayEnabled) {
          if (typeof Razorpay === 'undefined') {
            throw new Error('Razorpay checkout script did not load');
          }
          launchRazorpayCheckout(order, donorInput);
        } else {
          const { ok: simOk } = await postJSON('/api/simulate-payment', { donation_id: order.donation_id }, csrfToken);
          if (simOk) {
            goToReceipt(order.donation_id);
          } else {
            alert('Simulation failed');
            payBtn.disabled = false;
          }
        }
      } catch (err) {
        console.error('Donation submit failed:', err);
        payBtn.disabled = false;
        showStatusNote(
          'Something went wrong starting the payment. Please try again -- if it keeps happening, try ' +
          'reloading the page, a different browser, or contact the temple office.'
        );
      }
    });
  }

  return { init };
})();
