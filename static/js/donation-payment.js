/* Shared payment-flow logic for every public donation form
 * (Donate / Live To Give, Festival Seva, BACE Contribution).
 *
 * Rewritten from scratch. The previous version was functionally correct in
 * the happy path but had one real defect that produced the long-running
 * "have to click Continue twice" report -- see waitForRazorpay() below,
 * which is the substantive fix in this rewrite. Everything else here is a
 * cleaner restatement of behaviour that was already proven in production,
 * deliberately preserved rather than reinvented, because BACE Contribution
 * and Festival Seva were both independently confirmed working on it.
 *
 * ---------------------------------------------------------------------
 * The two-click bug
 * ---------------------------------------------------------------------
 * Razorpay's checkout.js is loaded from their CDN by a plain <script> tag
 * in each form template. The old code did:
 *
 *     if (typeof Razorpay === 'undefined') throw new Error(...)
 *
 * at submit time. That treats "the SDK hasn't finished downloading yet" as
 * a fatal error, when it's really just a race: the donor filled in the
 * form and clicked faster than a third-party CDN script finished loading.
 * The first click threw and showed "Something went wrong starting the
 * payment"; by the time the donor clicked again a second or two later the
 * script had arrived, so the second click worked. That exactly matches the
 * reported symptom, including why it hit the Live To Give page hardest
 * (it's by far the heaviest page -- hero image, photo gallery, "more ways
 * to give" cards -- so checkout.js finishes later relative to the donor)
 * and why it came and went day to day (pure network timing), and why it
 * was unaffected by the hosting plan (nothing to do with our own server).
 *
 * Now: waitForRazorpay() waits for the SDK, with a visible "preparing"
 * message, and only gives up after a genuine timeout.
 *
 * ---------------------------------------------------------------------
 * Confirmation has three layers (see public.py's module docstring for the
 * whole picture -- this file implements only layers 2 and 3):
 *   1. Webhook -- Razorpay's server calls ours directly. Source of truth.
 *      This file plays no part in it.
 *   2. Browser fast path -- checkout's `handler` callback posts to
 *      /api/verify-payment right after payment. Fires most of the time.
 *   3. Client polling -- fallback for when #2 doesn't fire, or fires and
 *      fails. Confirms nothing itself; only asks the server what layers 1
 *      and 2 have already recorded.
 * Layers 2 and 3 are both best-effort. A donation is never lost by this
 * file failing -- worst case the donor doesn't get *redirected*, while the
 * receipt still exists server-side. Every message shown on a failure path
 * is written with that in mind: never tell a donor their payment failed,
 * because this file is not in a position to know that.
 *
 * Usage, once per form template:
 *
 *   <script src="https://checkout.razorpay.com/v1/checkout.js"></script>
 *   <script src="{{ url_for('static', filename='js/donation-payment.js') }}"></script>
 *   <script>
 *     TempleDonationPayment.init({
 *       formId: 'my-form',
 *       description: 'Festival Seva',   // shown on the Razorpay modal
 *       orgName: {{ org_name | tojson }},
 *       razorpayEnabled: {{ razorpay_enabled | tojson }},
 *       beforeSubmit: function () { ... },  // optional
 *     });
 *   </script>
 */
window.TempleDonationPayment = (function () {
  'use strict';

  // ------------------------------------------------------------------
  // Tunables
  // ------------------------------------------------------------------

  // Post-payment confirmation poll: 100 x 3s = 5 minutes.
  // 2 minutes was tried first and proved too short in production more than
  // once -- donors saw "we couldn't confirm" for donations that had in fact
  // succeeded and been given a receipt number (verified in Admin ->
  // Donations Log). Paying by UPI app is the norm here, and it backgrounds
  // the browser tab, which browsers then throttle hard.
  const POLL_ATTEMPTS = 100;
  const POLL_INTERVAL_MS = 3000;

  // How long to wait for Razorpay's checkout.js before treating it as
  // genuinely unavailable rather than merely slow. 15s is far longer than
  // a normal load and still short enough not to feel broken.
  const SDK_WAIT_TIMEOUT_MS = 15000;
  const SDK_POLL_INTERVAL_MS = 100;

  // One silent retry when fetch() itself rejects (connection reset, TLS
  // hiccup, a cold first connection). A rejected fetch means the response
  // never arrived -- distinct from a response that arrived saying "no",
  // which is handled as a normal error, not retried.
  const FETCH_RETRY_DELAY_MS = 800;

  // "A payment was started and hasn't been confirmed in this browser yet."
  // Survives a full page reload, so an outcome isn't lost when the OS
  // discards the tab during a UPI app hand-off. Not scoped per form -- a
  // donor has at most one payment in flight at a time.
  const PENDING_KEY = 'templeDonationPending';
  // 30 minutes: comfortably longer than any realistic confirmation delay,
  // short enough that a donor returning much later to start a *new*
  // donation isn't yanked off to an old receipt.
  const PENDING_MAX_AGE_MS = 30 * 60 * 1000;

  // ------------------------------------------------------------------
  // Small helpers (module scope -- no per-form state)
  // ------------------------------------------------------------------

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  /* POST JSON and always resolve to {ok, data}. Never throws for an HTTP
   * error status -- only for a true network-level failure, and even then
   * only after one retry. */
  async function postJSON(url, body, csrfToken) {
    async function attempt() {
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify(body),
      });
      let data = {};
      try { data = await resp.json(); } catch (err) { /* non-JSON error page */ }
      return { ok: resp.ok, data: data };
    }
    try {
      return await attempt();
    } catch (err) {
      await sleep(FETCH_RETRY_DELAY_MS);
      return await attempt();
    }
  }

  /* GET the current server-side status of a donation. Resolves to a status
   * string, or null if it couldn't be determined right now (caller decides
   * whether that's worth reacting to -- usually it just means "try again
   * on the next tick"). */
  /* `verify` asks the server to additionally check with Razorpay directly
   * rather than only reporting what it has already been told (see
   * public._reconcile_pending_with_razorpay). Reserved for the two moments
   * where the cheap answer has already proven insufficient -- the poll
   * giving up, and the donor pressing "Check again" -- since it costs an
   * outbound API call, and firing it on every 3-second tick would be both
   * wasteful and rude to Razorpay's rate limits. */
  async function fetchDonationStatus(donationId, verify) {
    try {
      const url = '/api/donation-status/' + encodeURIComponent(donationId) + (verify ? '?verify=1' : '');
      const resp = await fetch(url);
      if (!resp.ok) return null;
      const data = await resp.json();
      return data && data.status ? data.status : null;
    } catch (err) {
      return null;
    }
  }

  function readPendingMarker() {
    try {
      const raw = localStorage.getItem(PENDING_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || !parsed.donationId) return null;
      if (Date.now() - (parsed.ts || 0) > PENDING_MAX_AGE_MS) return null;
      return parsed;
    } catch (err) {
      return null; // storage unavailable, or a corrupted marker
    }
  }

  function writePendingMarker(donationId) {
    try {
      localStorage.setItem(PENDING_KEY, JSON.stringify({ donationId: donationId, ts: Date.now() }));
    } catch (err) {
      // Private browsing / storage disabled. Resume-after-reload just
      // won't be available; not worth telling the donor about.
    }
  }

  function clearPendingMarker() {
    try { localStorage.removeItem(PENDING_KEY); } catch (err) { /* nothing to clear */ }
  }

  function goToReceipt(donationId) {
    clearPendingMarker();
    window.location.href = '/donate/success/' + encodeURIComponent(donationId);
  }

  /* Resolve once Razorpay's checkout.js has defined the global, or reject
   * after SDK_WAIT_TIMEOUT_MS. See the header comment -- this is the fix
   * for the "needs two clicks" bug. */
  function waitForRazorpay() {
    if (typeof window.Razorpay !== 'undefined') return Promise.resolve();
    return new Promise(function (resolve, reject) {
      const startedAt = Date.now();
      const timer = setInterval(function () {
        if (typeof window.Razorpay !== 'undefined') {
          clearInterval(timer);
          resolve();
        } else if (Date.now() - startedAt > SDK_WAIT_TIMEOUT_MS) {
          clearInterval(timer);
          reject(new Error('Razorpay checkout script did not load'));
        }
      }, SDK_POLL_INTERVAL_MS);
    });
  }

  // ------------------------------------------------------------------
  // Per-form setup
  // ------------------------------------------------------------------

  function init(config) {
    config = config || {};
    const formId = config.formId;
    const description = config.description;
    const orgName = config.orgName;
    const razorpayEnabled = config.razorpayEnabled;
    // Absolute URL (Razorpay loads it from their own page, so a relative
    // path wouldn't resolve). Optional -- checkout just shows no logo.
    const logoUrl = config.logoUrl;

    const form = document.getElementById(formId);
    if (!form) return; // this page doesn't have the form (campaign not configured)

    const payBtn = form.querySelector('#pay-btn') || document.getElementById('pay-btn');
    if (!payBtn) {
      // Bail loudly and early rather than letting the first
      // `payBtn.disabled = true` throw from inside the submit handler,
      // which would break the form with no clue as to why.
      console.error('TempleDonationPayment: no #pay-btn found for form', formId);
      return;
    }

    const csrfMeta = document.querySelector('meta[name="csrf-token"]');
    if (!csrfMeta) {
      console.error('TempleDonationPayment: no CSRF meta tag -- payment form will not work');
      return;
    }
    const csrfToken = csrfMeta.content;

    const statusNote = document.getElementById('status-note');

    // Guards against a donor managing to start two payments at once (double
    // submit, Enter key while the button is mid-flight). The button's own
    // disabled state mostly covers this; this covers the rest.
    let submitting = false;
    // At most one poll loop alive per page, so a dismissed-modal poll and a
    // verify-fallback poll can't both be running and fighting each other.
    let activePoll = null;
    // Set once the donor starts a new payment on this page. Anything still
    // in flight for an *earlier* donation must not redirect after this
    // point -- see the check in resumePendingDonation() and the
    // activePoll.stop() in the submit handler.
    let supersededByNewPayment = false;

    function setNote(text) {
      if (!statusNote) return;
      statusNote.textContent = text;
      statusNote.style.display = '';
    }

    function clearNote() {
      if (!statusNote) return;
      statusNote.textContent = '';
      statusNote.style.display = 'none';
    }

    /* Shown when a poll gives up. A delayed webhook can still land after
     * this point, so the donor gets a button to re-check rather than a
     * dead end whose only next step is phoning the office. */
    function setNoteWithRecheck(donationId, text) {
      if (!statusNote) return;
      statusNote.textContent = '';
      statusNote.style.display = '';
      statusNote.appendChild(document.createTextNode(text + ' '));

      const btn = document.createElement('button');
      // type="button" matters: this lives inside the donation <form>, and a
      // button with no explicit type defaults to submit -- which would
      // re-submit the form and start a *second* donation instead of just
      // checking on the first.
      btn.type = 'button';
      btn.className = 'btn btn-link p-0 align-baseline';
      btn.textContent = 'Check again';
      btn.addEventListener('click', async function () {
        btn.disabled = true;
        btn.textContent = 'Checking...';
        const status = await fetchDonationStatus(donationId, true);
        if (status === 'success') {
          goToReceipt(donationId);
          return;
        }
        btn.disabled = false;
        btn.textContent = 'Check again';
      });
      statusNote.appendChild(btn);
    }

    /* Poll until the donation resolves, we run out of attempts, or the
     * page goes away. Returns nothing; all outcomes are side effects.
     *
     * `quiet` suppresses the "confirming..." message, for the speculative
     * short poll after a donor dismisses the checkout modal -- most of
     * those are simple "changed my mind" dismissals with no payment
     * behind them, and shouldn't imply one is being processed. */
    function startPoll(donationId, options) {
      const attempts = options.attempts;
      const intervalMs = options.intervalMs;
      const quiet = !!options.quiet;
      const onGiveUp = options.onGiveUp;

      if (activePoll) activePoll.stop();

      let count = 0;
      let done = false;

      if (!quiet) {
        setNote('Confirming your payment... this can take a few minutes, especially if you paid ' +
                'via a UPI app. Please don\'t close this page.');
      }

      function stop() {
        if (done) return;
        done = true;
        clearInterval(timer);
        document.removeEventListener('visibilitychange', onVisible);
        if (activePoll && activePoll.stop === stop) activePoll = null;
      }

      async function checkOnce() {
        if (done) return;
        const status = await fetchDonationStatus(donationId);
        if (done) return; // resolved while this request was in flight
        if (status === 'success') {
          stop();
          goToReceipt(donationId);
        }
        // 'pending' -> keep waiting. 'failed'/'cancelled' -> also keep
        // waiting rather than declaring failure: a donation can sit at
        // 'failed' from one attempt while the donor immediately retries
        // and succeeds, and this file should never be the thing that
        // tells someone their payment failed.
      }

      // A UPI hand-off backgrounds this tab, and browsers throttle (or
      // effectively suspend) interval timers in background tabs -- so the
      // interval alone badly undercounts real elapsed time. Checking the
      // instant the tab comes back catches success right when the donor
      // returns, instead of whenever a throttled timer next fires.
      function onVisible() {
        if (document.visibilityState === 'visible') checkOnce();
      }
      document.addEventListener('visibilitychange', onVisible);

      const timer = setInterval(async function () {
        count += 1;
        if (count > attempts) {
          // Before giving up and showing the donor a worrying message,
          // ask Razorpay directly. Every "we could not confirm your
          // payment" report so far has turned out to be a donation that
          // actually succeeded, so this is precisely the moment to stop
          // waiting to be told and go and check.
          const finalStatus = await fetchDonationStatus(donationId, true);
          if (done) return;
          stop();
          if (finalStatus === 'success') {
            goToReceipt(donationId);
            return;
          }
          if (onGiveUp) onGiveUp();
          return;
        }
        checkOnce();
      }, intervalMs);

      activePoll = { stop: stop };
      // One immediate check, so an already-confirmed donation (common when
      // resuming after a reload) redirects without waiting a full tick.
      checkOnce();
    }

    const GIVE_UP_TEXT =
      'We could not confirm your payment automatically. If money was deducted, your receipt may ' +
      'still appear in "My Donations" once confirmation catches up -- please note the time and ' +
      'amount, and contact the temple office if it doesn\'t.';

    function pollThenGiveUp(donationId) {
      startPoll(donationId, {
        attempts: POLL_ATTEMPTS,
        intervalMs: POLL_INTERVAL_MS,
        onGiveUp: function () { setNoteWithRecheck(donationId, GIVE_UP_TEXT); },
      });
    }

    /* On page load: if a previous visit started a payment we never saw
     * resolve, pick it back up. Covers the case where the tab was
     * discarded entirely during a UPI hand-off -- otherwise the donor has
     * no path to their receipt except contacting the office. */
    (function resumePendingDonation() {
      const pending = readPendingMarker();
      if (!pending) {
        clearPendingMarker(); // also clears a stale/corrupt marker
        return;
      }
      fetchDonationStatus(pending.donationId).then(function (status) {
        // This request was already in flight if the donor started a new
        // payment in the meantime. Redirecting now would yank them off an
        // open checkout modal to an older receipt.
        if (supersededByNewPayment) return;
        if (status === 'success') {
          goToReceipt(pending.donationId);
        } else if (status === 'pending') {
          pollThenGiveUp(pending.donationId);
        } else if (status === null) {
          // Couldn't reach the server. Leave the marker alone so the next
          // page load tries again.
        } else {
          clearPendingMarker(); // failed/cancelled -- nothing to resume
        }
      });
    })();

    function launchCheckout(order, donorInput) {
      const options = {
        key: order.key_id,
        // round(), not truncation: float maths can land a hair under the
        // intended paise value, and the amount here must match the order
        // the server created exactly or checkout rejects it as a generic
        // "something went wrong". public.py rounds identically.
        amount: Math.round(order.amount * 100),
        currency: 'INR',
        name: orgName,
        description: description,
        // Razorpay's docs list `image` for the logo shown on the checkout
        // modal. Worth having on a donation form specifically: the donor
        // is handing money to a temple they may only know by name, and
        // seeing the same logo carry over from the page into the payment
        // window is reassurance that they're still in the right place.
        image: logoUrl || undefined,
        order_id: order.order_id,

        handler: async function (response) {
          // Razorpay calls this from its own code, long after our submit
          // handler returned -- so it needs its own try/catch. An
          // unguarded throw here would vanish silently, stranding a donor
          // who has already paid on a page that looks stuck.
          try {
            const result = await postJSON('/api/verify-payment', {
              donation_id: order.donation_id,
              razorpay_order_id: response.razorpay_order_id,
              razorpay_payment_id: response.razorpay_payment_id,
              razorpay_signature: response.razorpay_signature,
            }, csrfToken);

            if (result.ok) {
              goToReceipt(order.donation_id);
            } else {
              // The server said no. That is *not* proof the payment
              // failed -- the webhook is the source of truth and may
              // simply not have caught up. Fall through to polling.
              pollThenGiveUp(order.donation_id);
            }
          } catch (err) {
            // Network-level failure, already retried once inside
            // postJSON. Confirmed in production that the backend had
            // issued a receipt while the browser sat here, so polling is
            // the right response, not an error message.
            console.error('Payment verification call failed:', err);
            pollThenGiveUp(order.donation_id);
          }
        },

        modal: {
          ondismiss: function () {
            try {
              submitting = false;
              payBtn.disabled = false;
              // Usually a "changed my mind" dismissal with no payment
              // behind it -- but occasionally a donor closes the modal
              // moments after paying. A short quiet poll catches that
              // without implying anything is in progress if it isn't.
              startPoll(order.donation_id, { attempts: 5, intervalMs: 3000, quiet: true });
            } catch (err) {
              console.error('Error handling checkout dismissal:', err);
            }
          },
        },

        prefill: {
          name: donorInput.full_name,
          email: donorInput.email,
          contact: donorInput.phone,
        },
        theme: { color: '#1d3b6d' },
      };

      const rzp = new window.Razorpay(options);
      rzp.on('payment.failed', function (response) {
        // Razorpay shows the donor its own failure detail inside the
        // modal, so nothing is displayed from here -- competing messages
        // would only confuse. But the payload carries exactly the fields
        // that make a failure diagnosable after the fact (code, reason,
        // step, and the order/payment ids to look up in the Razorpay
        // dashboard), and throwing them away is how "it sometimes fails"
        // reports end up with nothing to go on. Logged defensively: the
        // error object is Razorpay's, and this must never itself throw
        // and leave the button stuck disabled.
        try {
          const err = (response && response.error) || {};
          const meta = err.metadata || {};
          console.error('Razorpay payment.failed:', {
            code: err.code,
            description: err.description,
            source: err.source,
            step: err.step,
            reason: err.reason,
            order_id: meta.order_id,
            payment_id: meta.payment_id,
            donation_id: order.donation_id,
          });
        } catch (e) {
          console.error('Razorpay payment.failed (unparseable payload)');
        }
        submitting = false;
        payBtn.disabled = false;
      });
      rzp.open();
    }

    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      if (submitting) return;

      if (typeof config.beforeSubmit === 'function') {
        try {
          config.beforeSubmit();
        } catch (err) {
          // A page-specific hook (e.g. Live To Give's first/last name ->
          // full_name sync) must never be able to block payment.
          console.error('beforeSubmit hook failed:', err);
        }
      }

      submitting = true;
      payBtn.disabled = true;
      clearNote();

      // Abandon anything still watching a previous, unconfirmed donation
      // (the resume-on-load check and its poll, most likely). Those hold
      // their own donation id in a closure, and if a late confirmation
      // lands while the donor is part-way through this new payment, they
      // would redirect to the *old* receipt -- pulling the page out from
      // under an open checkout modal. Starting a new payment supersedes
      // waiting on the old one; the pending marker below is overwritten
      // for the same reason.
      supersededByNewPayment = true;
      if (activePoll) activePoll.stop();

      try {
        const donorInput = Object.fromEntries(new FormData(form).entries());

        // Start the SDK wait *before* the network round-trip, so the two
        // overlap instead of running back to back. By the time the order
        // comes back, checkout.js has usually long since arrived, and this
        // resolves instantly.
        const sdkReady = razorpayEnabled ? waitForRazorpay() : Promise.resolve();
        // Nothing is awaiting sdkReady yet; without this a slow SDK would
        // count as an unhandled rejection before we get to the await below.
        sdkReady.catch(function () { /* handled at the await */ });

        const result = await postJSON('/api/create-order', donorInput, csrfToken);
        if (!result.ok) {
          // A real, specific, server-side "no" (amount below the minimum,
          // bad PAN, missing consent...). Show the server's own wording --
          // it's more useful than anything generic.
          setNote(result.data.error || 'Something went wrong. Please check your details and try again.');
          submitting = false;
          payBtn.disabled = false;
          return;
        }
        const order = result.data;

        if (!razorpayEnabled) {
          const sim = await postJSON('/api/simulate-payment', { donation_id: order.donation_id }, csrfToken);
          if (sim.ok) {
            goToReceipt(order.donation_id);
          } else {
            setNote(sim.data.error || 'Simulation failed.');
            submitting = false;
            payBtn.disabled = false;
          }
          return;
        }

        if (typeof window.Razorpay === 'undefined') {
          setNote('Preparing secure payment...');
        }
        await sdkReady;
        clearNote();

        // Deliberately set here, immediately before the checkout modal
        // opens, and NOT earlier at create-order time. The marker means
        // "a payment may be in progress" -- on the next page load it
        // triggers "Confirming your payment... please don't close this
        // page" and, failing that, "if money was deducted...". Writing it
        // before we know checkout will actually open means any failure
        // between here and there (most realistically the SDK wait timing
        // out) leaves a marker behind for a donation the donor never even
        // got the chance to pay for -- so their *next* visit opens with a
        // confirmation message about a payment that never happened.
        writePendingMarker(order.donation_id);
        launchCheckout(order, donorInput);
        // Deliberately leaves `submitting`/`payBtn` disabled here: the
        // checkout modal is now open and owns the interaction. They're
        // re-enabled by ondismiss or payment.failed.
      } catch (err) {
        console.error('Donation submit failed:', err);
        submitting = false;
        payBtn.disabled = false;
        setNote('Something went wrong starting the payment. Please try again -- if it keeps ' +
                'happening, try reloading the page or contact the temple office.');
      }
    });
  }

  return { init: init };
})();
