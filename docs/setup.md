# Setup: GoHighLevel, Claude, Slack and n8n

You need self-hosted n8n reachable over HTTPS, a GoHighLevel sub-account with a phone number that can send SMS, a calendar and a pipeline, an Anthropic API key and, optionally, a Slack incoming webhook. Plan about 45 minutes, most of it for the three GoHighLevel workflows.

## 1. GoHighLevel: token and IDs

### Private Integration token

1. Go to **Settings → Private Integrations → Create new integration**.
2. Give it these scopes (each one is the scope GoHighLevel's API docs list for the call):

   | Scope | Used for |
   |---|---|
   | `contacts.readonly` | Loading the patient and the list of their appointments |
   | `contacts.write` | Adding and removing tags, setting SMS DND after an opt out |
   | `calendars.readonly` | Free slots |
   | `calendars/events.readonly` | Reading the missed appointment back |
   | `calendars/events.write` | Booking the new visit |
   | `conversations.readonly` | Finding the patient's conversation |
   | `conversations/message.readonly` | Reading the last messages for Claude |
   | `conversations/message.write` | Sending the texts |
   | `opportunities.readonly` | Finding the patient's opportunity |
   | `opportunities.write` | Moving or creating the opportunity |

3. Copy the token (it starts with `pit-`) into `GHL_API_KEY`.

### IDs

- **Location ID** (`GHL_LOCATION_ID`): **Settings → Business Profile**.
- **Calendar ID** (`GHL_CALENDAR_ID`): open the calendar's settings; the ID is in the page URL. Or list the calendars with your token (calendar calls use `Version: 2021-04-15`) and copy the `id`:

  ```bash
  curl "https://services.leadconnectorhq.com/calendars/?locationId=YOUR_LOCATION_ID" \
    -H "Authorization: Bearer pit-your-token" -H "Version: 2021-04-15"
  ```

  This is the calendar patients book into. No-shows in other calendars are ignored, and rebooked visits go into this calendar.
- **Pipeline and stage IDs** (`GHL_PIPELINE_ID`, `GHL_NO_SHOW_STAGE_ID`, `GHL_REBOOKED_STAGE_ID`): first add a **No-show** and a **Rebooked** stage to your patient pipeline (**Opportunities → Pipelines**), then list them:

  ```bash
  curl "https://services.leadconnectorhq.com/opportunities/pipelines?locationId=YOUR_LOCATION_ID" \
    -H "Authorization: Bearer pit-your-token" -H "Version: 2021-07-28"
  ```

  Copy the pipeline `id` and the `id` of both stages.

  After a no-show, the agent moves the patient's open opportunity in this pipeline (or else a lost or abandoned one) to No-show, and later to Rebooked. A won opportunity is a past sale and is never touched; the agent creates a new opportunity instead. That only works if **Allow Multiple Opportunities per Contact** is on for the sub-account (**Settings → Objects → Opportunities**). If it is off, GoHighLevel refuses the new opportunity, the patient is still texted, and the alert asks you to add the opportunity by hand.

### Same physio

With `SAME_STAFF=true` (the default), new times are asked for with the `userId` of the missed visit's team member, and the booking keeps that team member. Use a calendar that has its team members set up (a round robin or team calendar). If the missed visit had no team member, any open time is offered.

### Calendar notifications

The rebooked visit is created with `toNotify: true`, so GoHighLevel's automations run for it (the API docs say `false` would stop them). Turn off calendar SMS notifications that duplicate the reminder workflow, and check the calendar's "Cancellation Email", which HighLevel also sends when a visit is marked no-show. Details in [ghl/native-workflows.md](../ghl/native-workflows.md).

Only text people who gave you permission to text them. In the US, sending through a GoHighLevel number also needs A2P 10DLC registration.

## 2. Anthropic

Create a key at **console.anthropic.com → API keys** and put it in `ANTHROPIC_API_KEY`. The default model is `claude-sonnet-5`. `claude-haiku-4-5` (cheaper) and `claude-opus-5` also work. Replies use a forced tool call, which needs thinking turned off, so `claude-opus-5-5` and the Claude Fable and Mythos models do not work: they refuse the forced tool call (checked with Anthropic's token counting endpoint for `claude-opus-5-5` and `claude-fable-5-1`), and Anthropic's model docs say they also refuse thinking turned off. With one of them set, both webhooks answer 500 and name the problem.

## 3. Slack (optional)

Create an incoming webhook (**api.slack.com/apps → your app → Incoming Webhooks**) for a private channel and put its URL in `SLACK_WEBHOOK_URL`. It receives rebookings, hand offs, the daily summary and the error alerts. Alerts include patients' names and phone numbers, so keep the channel private.

## 4. n8n

1. Copy `.env.example` into your n8n environment, fill in the values and restart n8n. The nodes read them with `$env`, so self-hosted n8n needs `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`. (n8n Cloud does not allow `$env`. There, replace `$env.` with `$vars.` and create the same names under Variables.)
2. Make up a long random string for `GHL_WEBHOOK_SECRET`, for example with `openssl rand -hex 32`. You will paste the same value into GoHighLevel. The workflow refuses to run with a secret shorter than 16 characters or with the example value from `.env.example`.
3. Import `workflow/error-alert-slack.json` and **activate it**. n8n 2.x only runs an error workflow that is active; an inactive one is skipped without any alert.
4. Import `workflow/ghl-no-show-rebooking-agent.json`. In its settings, choose the error alert workflow as its **Error Workflow**.
5. Activate the no-show workflow.

It now listens on two production URLs and runs on two schedules:

- `https://YOUR-N8N-DOMAIN/webhook/ghl-no-show` receives no-shows
- `https://YOUR-N8N-DOMAIN/webhook/ghl-no-show-reply` receives replies
- every 15 minutes: texts that waited for quiet hours, follow-ups and closing silent conversations
- every day at 6 PM in n8n's time zone (`GENERIC_TIMEZONE`): the Slack summary. Change the hour in the **Schedule: Daily Summary** node

## 5. GoHighLevel workflows

Build the three native workflows from [ghl/native-workflows.md](../ghl/native-workflows.md):

1. **Harbor Physio: confirmation and reminders** (Appointment Status, New and Confirmed)
2. **Harbor Physio: no-show to rebooking agent** (Appointment Status, No-show, a 15 minute wait, then a Custom Webhook to `/webhook/ghl-no-show`)
3. **Harbor Physio: no-show replies to rebooking agent** (Customer Replied, SMS, tag `no-show`, Custom Webhook to `/webhook/ghl-no-show-reply`)

Both Custom Webhook actions use **Authorization: Bearer Token** with your `GHL_WEBHOOK_SECRET`. Treat that secret like a password: anyone who has it can make the agent text your patients. If it leaks, change it in n8n and in both GoHighLevel workflows.

Turn off anything else that answers these patients automatically, such as a Conversation AI bot, or they get two replies.

## 6. First test

Use your own phone number on a test contact.

1. Book a visit for yourself in the calendar, then mark it **No Show** in the appointment list. After the 15 minute wait you get the first text with three open times (or, outside 9 AM to 7 PM in your contact's time zone, the next morning). In GoHighLevel the contact has the `no-show` tag and the opportunity is in the No-show stage.
2. Reply "2". The new visit appears on the calendar, the opportunity moves to Rebooked, the `no-show` tag is replaced by `no-show-rebooked`, Slack gets a rebooking alert, and workflow 1 starts for the new visit without sending its own confirmation.
3. On a second test contact, reply "Do you have anything Friday morning?" and check that the times match. Then reply "STOP" and check that SMS DND is on.
4. To try a follow-up without waiting a day, set `FOLLOW_UP_HOURS=0.1` for a moment (6 minutes), restart n8n, and let the 15 minute run pick it up. Set it back afterwards.

## 7. Tags the agent adds

With the default `TAG_PREFIX`:

| Tag | Meaning |
|---|---|
| `no-show` | In a rebooking conversation. Removed when the conversation ends |
| `no-show-agent-booked` | Set a moment before the agent books. Workflow 1 removes it |
| `no-show-rebooked` | Rebooked, by text or another way |
| `no-show-declined` | Did not want to rebook now |
| `no-show-no-reply` | No answer after the follow-ups |
| `no-show-needs-human` | A person should take over. While the tag is on, the agent ignores the patient's texts and does not text them after another no-show. Remove it when you are done with the patient |
| `no-show-opted-out` | Replied STOP or asked to stop. SMS DND is on |

## Troubleshooting

Open the workflow's **Executions** list in n8n:

| You see | Likely cause |
|---|---|
| Run stopped at "Stop: Bad Secret" | The Bearer Token in the GoHighLevel Custom Webhook does not match `GHL_WEBHOOK_SECRET`, or the free Webhook action has no `secret` custom data |
| A webhook answered 500, run stopped at "Stop: Bad Settings" | A setting is missing or invalid (also a `GHL_WEBHOOK_SECRET` under 16 characters or left at the example value). The response and the alert list every problem |
| Run stopped at "Stop: Bad Payload" | The GoHighLevel action sent the right secret but no contact id. Check that its `contactId` value is the Contact ID merge field (`{{contact.id}}`) |
| A run failed but Slack stayed quiet | The error alert workflow is not active, is not chosen as the Error Workflow, or `SLACK_WEBHOOK_URL` is empty |
| Run ended at "Record Skip" | The no-show was not texted on purpose. The item says why: no longer a no-show, cancelled, already rebooked, SMS DND, a person looking after them (the needs-human tag), another calendar, a repeat. The daily summary counts these |
| Run stopped at "Stop: No-Show Lookup Failed" or "Stop: Patient Lookup Failed" | `GHL_API_KEY` scopes (`contacts.readonly`, `calendars/events.readonly`) or `GHL_LOCATION_ID` |
| Run stopped at "Stop: Tag Failed" | The `contacts.write` scope. Nobody was texted; text the patient by hand |
| No text after a no-show, run ended at "No-Show Failed?" | Quiet hours in the patient's time zone. The text waits in the agent's memory and the 15 minute run sends it when the window opens |
| Replies never reach n8n | Workflow 3 filters on the `no-show` tag and the SMS channel; check that the patient has the tag and that the workflow is published |
| Reply run ended at "Route Reply" | Ignored on purpose: a repeat delivery, no open conversation, SMS DND or a person took over |
| Run stopped at "Stop: Reply Needs Attention" | Read the message. It says what the patient got (a reply, a holding reply, or nothing when their message may be an opt out) and which step to finish by hand |
| "without the conversation history" | The patient was answered, but the conversation could not be loaded. Check the `conversations.readonly` and `conversations/message.readonly` scopes |
| "URGENT: ... asked to stop texts" | GoHighLevel did not accept the DND update. Set DND for SMS on that contact by hand right away |
| "was not moved to the No-show stage" or "the Rebooked stage" | Usually a won opportunity with multiple opportunities per contact turned off. Move or add it by hand |
| Run stopped at "Stop: Tick Needs Attention" | The 15 minute run hit problems. The message lists each patient and what to do. "did not find this contact in 3 runs in a row" means the contact was probably deleted or merged; the agent has let go of it |
| Slack: "No-show patient already rebooked" | The patient booked another way (by phone, online or through the team) and then texted. The agent did not answer or book; reply by hand if their message needs it |
| The reminder workflow sent its own confirmation after a rebooking | Workflow 1's If/Else does not see the `no-show-agent-booked` tag, or the flag tag could not be added (the alert says so) |
