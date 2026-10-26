# Changelog

## v1.1 (October 27, 2026)

An adversarial review before publishing re-read every node, the mocks and the docs, checked the request and response shapes again against GoHighLevel's OpenAPI files, and re-checked the riskiest GoHighLevel facts against the help center. It found and fixed:

- **A patient who booked by phone could still get the morning text.** A first text that waited for quiet hours to end was sent after a reload that only checked DND, so a patient who called the clinic at 8 AM and rebooked still got "We missed you, would one of these times work?" at 9. Waiting texts of open conversations now go through the same checks as follow-ups: a visit booked another way or a person taking over closes the conversation and drops the text.
- **A reply could book a second visit.** Nothing in the reply path looked at the patient's appointments, so a patient who rebooked by phone and then answered "2" got a second booking. Every reply now loads the patient's appointments first; if they have a visit booked after the missed one, the agent does not answer or book, closes the conversation as rebooked and posts a Slack note with their words.
- **A waiting first text that failed in the morning left the patient stuck.** The case stayed open with no text and no follow-ups, was never closed, and every later no-show of that patient was skipped as "already in a rebooking conversation". It now ends the conversation (as a failed first text at no-show time already did), and the alert still carries the text to send by hand. Open cases with no text at all are also closed after `CLOSE_AFTER_HOURS`.
- **A patient a person was looking after got texted again.** The needs-human tag stays on the contact, and the agent ignores the texts of tagged patients, so after a later no-show the patient got times offered and their "1" went unanswered. A no-show for a patient with that tag is now skipped and counted in the daily summary.
- **The "already rebooked" check depended on an undocumented time zone.** The contact appointment list has times without an offset and GoHighLevel does not say which zone they are in; they were compared with the missed visit's time from another endpoint. They are now compared with the missed visit's own entry in the same list, so the zone no longer matters. The missed visit's time is also read with the workflow's parser everywhere (a time without an offset is read in `BUSINESS_TZ`, not in the n8n server's zone). The mock now serves the list in a zone 5 hours off the clinic's, and a patient with an earlier visit the same morning proves it.
- **A webhook call with the right secret but no contact id was dropped quietly.** A merge field that did not fill (or the unconfirmed contact id key of the free Webhook action) made every no-show disappear with a 200 and no alert. Such a call still gets 200, then fails the run so the error alert fires.
- **The example secret from `.env.example` was accepted.** Anyone who read this repo could have called a copy that kept it. The settings check now refuses `GHL_WEBHOOK_SECRET` when it is the example value or shorter than 16 characters.
- **Every run rewrote the agent's memory.** n8n saves workflow static data after any run that assigns to it, and each node assigned it ("store.cases = store.cases || {}"), so even an idle 15 minute run or an ignored reply wrote the whole memory back and could overwrite a reply handled at the same moment. Missing parts are now created only when missing, so runs that change nothing do not write. (Checked in n8n 2.19.5's own code; the race between two runs that both change the memory remains and is described in the README.)
- **A deleted or merged contact failed the 15 minute run for good.** The run failed and alerted every 15 minutes, forever. After 3 runs in a row in which GoHighLevel answers 400, 404 or 422 for the contact, the agent lets go of the case and says so once. Other errors (token, outage) are still retried and reported every time.
- **A late follow-up pulled the next step right behind it.** After quiet hours or n8n downtime, the second follow-up (or the close) could follow 15 minutes after a late first one. Each step now also keeps its configured distance from the text before it.
- **The daily summary called texts held for quiet hours "texted after quiet hours"**, which is no longer true when such a text is dropped or fails. It now says "held for the morning".
- **Recipe corrections from the help center.** Workflow 2's wait is "A set period of time" (not a time delay option); the Custom Webhook is added with + Add action → Send Data → Custom Webhook, and its POST event is a form-style payload with no Content-Type field (the recipe said to pick `application/json` if offered). The Reply Channel label **SMS** and `{{message.body}}` holding the reply in a Customer Replied workflow are now confirmed by HighLevel's SMS forwarding article; the open points list was updated.

Unchanged after review: the Claude request (so the token counts from v1 still apply), the API request shapes (all match the OpenAPI files), and the quiet hours logic for follow-ups and replies.

## v1 (October 26, 2026)

First build: a no-show rebooking agent for appointment businesses. The GoHighLevel side runs as three native workflows (recipe in `ghl/native-workflows.md`); the checks, the texts, the Claude conversation and the bookings run in n8n, with an error alert workflow and an end to end mock test (53 scenarios, 75 checks).

### GoHighLevel workflows (recipe)

- Confirmation and reminders on the Appointment Status trigger (New, Confirmed), with waits for "An upcoming appointment or booking" 24 hours and 2 hours before the visit, and an If/Else that skips the confirmation for visits the agent booked (tag `no-show-agent-booked`).
- A no-show workflow on Appointment Status = No-show: a 15 minute wait, then a Custom Webhook (Bearer Token) to n8n. The free Webhook action works too, with the secret as custom data.
- A reply workflow on Customer Replied (SMS, tag `no-show`) that forwards each reply with a Custom Webhook.
- Every fact was checked against HighLevel's help center and API docs, and each one is labeled confirmed, partial or not confirmed, with the open points listed at the end.

### No-shows

- The webhook checks the shared secret (constant time comparison) and every setting first, and answers 200 at once.
- The missed visit is read back from GoHighLevel: the payload's appointment ID when the free Webhook action sends one, otherwise the patient's newest no-show in the calendar. Nothing is sent when the status changed during the wait, the visit was cancelled, the patient already booked another visit, SMS DND is on, they opted out earlier, they have no phone, or a conversation is already open. Repeats are ignored by appointment ID.
- The first text is kind, with no blame and no fees (unless `NO_SHOW_POLICY_TEXT` is set), and offers 3 real open times from the free slots endpoint, one per day, with the same team member and the same length as the missed visit.
- The patient is tagged `no-show` before the text goes out, and the opportunity moves to the No-show stage (open first, then lost or abandoned; a won one is never touched; otherwise a new one is created).
- Texts only go out between `SEND_HOUR_START` and `SEND_HOUR_END` in the patient's time zone. Outside those hours the text waits in the agent's memory and its times start at least 2 hours after the window opens.

### Replies

- Reads the Custom Webhook body (JSON or form fields), the free Webhook action's payload and the InboundMessage format. Repeat deliveries are matched to the newest message with the same words from the last 24 hours.
- STOP and the other opt out words set SMS DND in code before any AI call, keeping the contact's other DND channels.
- Claude (`ANTHROPIC_MODEL`, default `claude-sonnet-5`) picks one of six intents through a forced, strict tool call: picked an offered time, wants other times (with the days and time of day they asked for), wants a call or has a question, does not want to rebook, stop, or unclear. Patient text only appears inside tags in the user turn.
- A picked time is booked (flag tag first, `toNotify` on) and the opportunity moves to Rebooked. Other times are real open times that match the request and that the patient has not seen. A call or a question goes to a person with a Slack alert. "No thanks" gets a kind close.
- After `MAX_AI_REPLIES` agent replies a person takes over. Claude failures (an error, no tool call, `max_tokens`) send a holding reply, unless the message may be an opt out.
- At night, the decision and any booking happen at once and the text waits for the morning.

### Every 15 minutes, and daily

- Sends texts that waited for quiet hours to end, up to two follow-ups (`FOLLOW_UP_HOURS`, default 24 and 72 hours) with fresh times, and closes silent conversations after `CLOSE_AFTER_HOURS` with the `no-show-no-reply` tag.
- Each patient is reloaded first: DND, a person taking over or a booking made another way closes the conversation instead.
- A daily Slack summary of no-shows, rebooked, waiting, handed to a person, declined, no reply and opted out.

### Found and fixed while testing

- **Every no-show stopped in the first Code node that built a query string.** `URLSearchParams` does not exist in the sandbox n8n 2.x runs Code nodes in. Query strings are now built by a small helper.
- **The 15 minute run failed on every patient.** In the loop, `$('GHL: Reload Patient').first()` read the node's error output, because that is the output wired straight into the checking node, so a patient who loaded fine looked empty. The node's success output is now read by index.
- **A no-show without a matching appointment never reached the daily summary.** The skip record had no contact ID on that branch, so it was dropped. It now carries the contact and is counted.
- **The daily summary called texts that were still waiting for the morning "texted".** It now counts them separately ("texted after quiet hours").
- **A text waiting for the morning could stay in the agent's memory forever** when the patient opted out before it was sent, because housekeeping kept every case with a waiting text. Closing a conversation now drops its waiting text.
- **The test calendar was unrealistic.** Every patient was offered the same three times, so later patients' bookings were refused as taken. The workflow handled that correctly (honest text, hand off), but the scenarios meant to test other things failed. The mock calendar now has three physios per time slot, like a small clinic.
- **A check was weaker than its name.** The check that a waiting text's times start at least 2 hours after the morning did not look at the times. It does now.
- **The test expected different wording.** The first-text checks expected "Reply 1, 2 or 3, or tell us a day"; the workflow writes "Reply 1, 2 or 3 to book, or tell us a day", which is clearer, so the test was changed.
