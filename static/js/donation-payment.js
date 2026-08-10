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

  // How long the post-payment confirmation poll keeps trying before it
  // gives up and shows the "couldn't confirm automatically" message.
  // 40 attempts (2 minutes) turned out not to be long enough in practice:
  // twice now, a donor saw that give-up message while the payment had, in
  // fact, already succeeded on the backend (confirmed via Admin ->
  // Donations Log) -- the webhook just hadn't caught up within that
  // window yet, most plausibly because completing payment via a UPI app
  // (extremely common in India) backgrounds this browser tab, and
  // browsers throttle background timers hard, and/or the UPI app hand-off
  // + webhook delivery simply took a little over 2 minutes. 5 minutes
  // gives real-world confirmation delays a lot more room before a donor
  // sees a scary message about something that actually went fine.
  const CONFIRMATION_POLL_ATTEMPTS = 100;
  const CONFIRMATION_POLL_INTERVAL_MS = 3000;

  // localStorage key for "a donation was started and we haven't confirmed
  // it succeeded yet" -- see resumePendingDonation() below. Not scoped per
  // form/page since a donor only has one payment in flight at a time in
  // one browser.
  const PENDING_KEY = 'templeDonationPending';
  const PENDING_MAX_AGE_MS = 6 * 60 * 60 * 1000; // 6 hours -- older than this, not worth resuming

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

  function clearPendingMarker() {
    try { localStorage.removeItem(PENDING_KEY); } catch (err) { /* storage unavailable -- nothing to clear */ }
  }

  function setPendingMarker(donationId) {
    try {
      localStorage.setItem(PENDING_KEY, JSON.stringify({ donationId: donationId, ts: Date.now() }));
    } catch (err) {
      // Privacy mode / storage disabled -- the resume-on-reload feature
      // just won't be available this session, not worth surfacing to the
      // donor over.
    }
  }

  function goToReceipt(donationId) {
    clearPendingMarker();
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
      let finished = false;
      if (!quiet) showStatusNote('Confirming your payment... this can take a few minutes, especially if you paid via a UPI app. Please don\'t close this page.');

      async function checkOnce() {
        if (finished) return;
        try {
          const resp = await fetch(`/api/donation-status/${donationId}`);
          const status = await resp.json();
          if (status.status === 'success' && !finished) {
            finished = true;
            clearInterval(timer);
            document.removeEventListener('visibilitychange', onVisibilityChange);
            goToReceipt(donationId);
          }
        } catch (err) {
          // Transient network hiccup -- just try again on the next tick.
        }
      }

      // Completing payment via a UPI app (very common in India) typically
      // backgrounds this browser tab for the donor to approve it in a
      // separate app -- and browsers throttle (sometimes almost entirely
      // pause) setInterval timers in background tabs to save battery, so
      // the regular interval below can badly undercount real elapsed
      // time. Checking immediately the moment the tab becomes visible
      // again catches success right when the donor comes back, instead of
      // waiting for a throttled timer to eventually get around to it.
      function onVisibilityChange() {
        if (document.visibilityState === 'visible') checkOnce();
      }
      document.addEventListener('visibilitychange', onVisibilityChange);

      const timer = setInterval(async () => {
        count += 1;
        if (count > attempts) {
          finished = true;
          clearInterval(timer);
          document.removeEventListener('visibilitychange', onVisibilityChange);
          if (onGiveUp) onGiveUp();
          return;
        }
        await checkOnce();
      }, intervalMs);
    }

    // Runs once on page load, before anything else. If a previous visit to
    // this form started a payment whose outcome was never confirmed in the
    // browser (the classic case: the tab got backgrounded or even reloaded
    // by the OS while the donor was in a UPI app, and came back too late
    // or never came back to a live poll), this picks that donation back up
    // -- checks it once immediately, and if it's still pending, resumes
    // polling rather than leaving the donor with no path to ever finding
    // out except by checking with the office. Silently does nothing if
    // there's no marker, it's stale, or the donation already resolved.
    function resumePendingDonation() {
      let pending;
      try {
        const raw = localStorage.getItem(PENDING_KEY);
        if (!raw) return;
        pending = JSON.parse(raw);
      } catch (err) {
        return; // storage unavailable or corrupted marker -- nothing to resume
      }
      if (!pending || !pending.donationId || (Date.now() - (pending.ts || 0)) > PENDING_MAX_AGE_MS) {
        clearPendingMarker();
        return;
      }

      fetch(`/api/donation-status/${pending.donationId}`)
        .then((resp) => resp.json())
        .then((status) => {
          if (status.status === 'success') {
            goToReceipt(pending.donationId);
          } else if (status.status === 'pending') {
            pollDonationStatus(pending.donationId, {
              attempts: CONFIRMATION_POLL_ATTEMPTS, intervalMs: CONFIRMATION_POLL_INTERVAL_MS, quiet: false,
              onGiveUp: () => showStatusNote(
                'We could not confirm a previous payment automatically. If money was deducted, please note ' +
                'the time and amount and contact the temple office, or check "My Donations" shortly -- your ' +
                'receipt may still appear there once confirmation catches up.'
              ),
            });
          } else {
            // failed/cancelled -- nothing left to resume
            clearPendingMarker();
          }
        })
        .catch(() => { /* couldn't check right now -- leave the marker for the next page load to try again */ });
    }
    resumePendingDonation();

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
                attempts: CONFIRMATION_POLL_ATTEMPTS, intervalMs: CONFIRMATION_POLL_INTERVAL_MS, quiet: false,
                onGiveUp: () => showStatusNote(
                  'Still waiting on confirmation from the payment gateway. If money was deducted, your ' +
                  'receipt will appear in "My Donations" shortly, or contact the temple office.'
                ),
              });
            }
          } catch (err) {
            // The verify-payment call itself failed at the network level
            // (fetch() threw -- a dropped connection, not a clean error
            // response). That does NOT mean the payment failed: the
            // webhook (the actual source of truth, entirely independent
            // of this browser tab) may already have recorded it as
            // successful, and confirmed exactly that happening in
            // production once already -- the backend had a receipt
            // issued while the donor was still staring at this catch
            // block with no way to know that. Falling back to polling
            // here, same as the "verify-payment returned an error"
            // branch above, means the donor still gets redirected to
            // their receipt automatically once the webhook (or a retry)
            // catches up, instead of being stuck on a dead page.
            console.error('Payment verification failed:', err);
            pollDonationStatus(order.donation_id, {
              attempts: CONFIRMATION_POLL_ATTEMPTS, intervalMs: CONFIRMATION_POLL_INTERVAL_MS, quiet: false,
              onGiveUp: () => showStatusNote(
                'We could not confirm your payment automatically. If money was deducted, please note the ' +
                'time and amount and contact the temple office, or check "My Donations" shortly -- your ' +
                'receipt may still appear there once confirmation catches up.'
              ),
            });
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

        // Marks this donation as "in flight" before handing off to
        // Razorpay -- if this tab gets backgrounded or reloaded during a
        // UPI app hand-off and the donor comes back (or opens a fresh
        // tab) later, resumePendingDonation() on the next page load picks
        // this back up instead of the outcome being lost entirely.
        setPendingMarker(order.donation_id);

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
