# Native GoHighLevel workflows: the recipe

The GoHighLevel side of this build runs as three native workflows (**Automation → Workflows → Create Workflow → Start from scratch**). This page lists everything needed to build them by hand in a sub-account: triggers and filters, every action in order with its settings and message text, the tags, and the webhook calls to n8n.

Each fact about GoHighLevel was checked against HighLevel's help center or API docs on October 26, 2026, and the riskiest ones (the Appointment Status trigger, the waits, the Custom Webhook action, the Customer Replied trigger and its message merge field) again on October 27, 2026. Labels:

- **Confirmed**: a HighLevel page states it (source linked).
- **Partial**: a HighLevel page implies it or only covers part of it.
- **Not confirmed**: no official page says so. Check it in your sub-account while you build.

The full list of open points is at the end.

| # | Workflow | Trigger | What it does |
|---|---|---|---|
| 1 | Harbor Physio: confirmation and reminders | Appointment Status (New, Confirmed) | Confirms a booking, then reminds 24 hours and 2 hours before the visit |
| 2 | Harbor Physio: no-show to rebooking agent | Appointment Status (No-show) | Waits 15 minutes, then tells n8n about the no-show |
| 3 | Harbor Physio: no-show replies to rebooking agent | Customer Replied (SMS, tag `no-show`) | Forwards each SMS reply from a patient in a rebooking conversation to n8n |

"Harbor Physio Clinic" is the made-up business used throughout this repo. Use your own names.

## Before you build

### Tags

| Tag | Added by | Used by |
|---|---|---|
| `no-show` | n8n, when it texts a patient after a no-show. Removed again when the conversation ends | Workflow 3 filter (only these patients' replies go to n8n) |
| `no-show-agent-booked` | n8n, a moment before it books a rebooked visit | Workflow 1: skips its own confirmation, because the agent already confirmed by text, then removes the tag |
| `no-show-needs-human` | n8n, when a person should take over | n8n itself: while the tag is on, the agent ignores the patient's texts and does not text them after a new no-show. Remove it when you are done with the patient |
| `no-show-rebooked`, `no-show-declined`, `no-show-no-reply`, `no-show-opted-out` | n8n, when the conversation ends | Your smart lists and reports. No workflow needs them |

n8n adds these tags through the API. So that `no-show` and `no-show-agent-booked` can be picked in the filters of workflows 1 and 3 before n8n has used them, add both to a test contact once (by hand) before you build. If you change `TAG_PREFIX` in n8n, all tag names change with it.

### Custom fields and custom values

None are needed. The agent keeps its own conversation memory in n8n and reads everything else (appointment, calendar, opportunity) through the API.

### Pipeline

The opportunity pipeline needs a **No-show** stage and a **Rebooked** stage (for example New, Booked, No-show, Rebooked, Lost). Their IDs go into `GHL_NO_SHOW_STAGE_ID` and `GHL_REBOOKED_STAGE_ID`; [docs/setup.md](../docs/setup.md) shows the API call that lists them.

### Calendar notifications

Check the calendar's own notifications so patients are not texted twice. HighLevel's calendar "Cancellation Email" is "Sent when an appointment is canceled, marked as no-show, or deemed invalid", and a "Reschedule Email" goes out when the time changes. **Confirmed**, [appointment notifications](https://help.gohighlevel.com/support/solutions/articles/155000003441-calendar-email-in-app-sms-whatsapp-appointment-notifications) and [appointment scenarios in workflows](https://help.gohighlevel.com/support/solutions/articles/155000002697-appointment-scenarios-in-workflow). Turn off any calendar SMS confirmation or reminder that duplicates workflow 1.

### How staff mark a no-show

In the calendar's Appointment List View, "Click the Status dropdown in the appointment row to select the appropriate status, such as Confirmed, Showed, No Show, Cancelled, or Invalid." **Confirmed**, [appointment list view](https://help.gohighlevel.com/support/solutions/articles/155000003447-how-to-manage-appointments-with-appointment-list-view). The step by step for the appointment drawer in the calendar view was not found (**partial**).

## Workflow 1: Harbor Physio: confirmation and reminders

### Trigger: Appointment Status

| Setting | Value | Source |
|---|---|---|
| Trigger | **Appointment Status** ("activates workflows when an appointment is scheduled or its status changes") | **Confirmed**, [trigger article](https://help.gohighlevel.com/support/solutions/articles/155000002619-workflow-trigger-appointment-status) |
| Filter: In Calendar | Your visits calendar (the one in `GHL_CALENDAR_ID`) | **Confirmed**, same page |
| Filter: Filter By Appointment Status | **New** and **Confirmed** | **Confirmed** values: New, Confirmed, Cancelled, Showed, No-show, Invalid |

Why both New and Confirmed: HighLevel's re-entry rule says "A contact can re-enter the workflow after they have fully completed it or have been manually removed" (**confirmed**, [workflow settings](https://help.gohighlevel.com/support/solutions/articles/48001239875-workflow-settings-overview)). A booking that starts as New and is confirmed later should therefore not enter twice while it is still waiting in this workflow. That reading of the rule is **partial**: test it once with a booking you confirm by hand.

Customer Booked Appointment is not used, because "Manually booked appointments will not trigger the workflow" (**confirmed**, [trigger article](https://help.gohighlevel.com/support/solutions/articles/155000002675-workflow-trigger-customer-booked-appointment)), and the rebooked visits are created by n8n through the API.

### Settings

| Setting | Value | Why |
|---|---|---|
| Allow Re-entry | On | Patients book many visits. **Confirmed** setting |
| Time Window | Monday to Saturday, 9:00 AM to 7:00 PM (your opening hours) | "If an action is scheduled to run outside the defined window, it will be paused and will resume at the next available time slot." **Confirmed** |
| Timezone | The clinic's time zone | The workflow timezone controls Wait and Time Window steps. **Confirmed** |
| Stop on Response | Off | A reply to a reminder should not cancel the next reminder |

### Actions, in order

1. **If/Else**, name it "Booked by the agent?". Branch **Yes**: Contact Tag includes `no-show-agent-booked`. Otherwise the **None** branch runs. (**Partial**: HighLevel's [Go To article](https://help.gohighlevel.com/support/solutions/articles/155000003377-workflow-action-go-to) shows an If/Else on a contact tag, "Add if else condition to check if the user has "Purchased" tag". The exact operator label, for example "includes", is not on an official page.)
   - **Yes branch:**
     1. **Remove Contact Tag**: `no-show-agent-booked` (**confirmed** action, [remove contact tag](https://help.gohighlevel.com/support/solutions/articles/155000003266-workflow-action-remove-contact-tag)). No confirmation text: the rebooking agent already confirmed the visit in the SMS conversation.
     2. **Go To**: step 2 below ("Wait: 24 hours before"). (**Confirmed** action, [go to](https://help.gohighlevel.com/support/solutions/articles/155000003377-workflow-action-go-to).)
   - **None branch:**
     1. **Send SMS** (**confirmed** action, [send SMS](https://help.gohighlevel.com/support/solutions/articles/155000002474-workflow-action-send-sms)), message:
        ```
        Hi {{contact.first_name}}, your visit at Harbor Physio Clinic is confirmed for {{appointment.start_time}}. Reply here if you need to change it.
        ```
     2. Continue to step 2.
2. **Wait**, name it "Wait: 24 hours before". Wait for **An upcoming appointment or booking**, Type **Appointment / Calendar Event**, "When should the Contact proceed?" **Before**, **1 day** (or 24 hours). "If this date has already passed": **Skip all outbound communication actions till next wait or event start date action**. (**Confirmed**, [wait action](https://help.gohighlevel.com/support/solutions/articles/155000002470-action-wait-time-delay), page updated September 7, 2026. The option used to be called "Event/Appointment Time".) This way a booking made a few hours before the visit gets no "tomorrow" reminder.
3. **Send SMS**, message:
   ```
   Hi {{contact.first_name}}, a reminder of your physio appointment tomorrow at {{appointment.only_start_time}} at Harbor Physio Clinic. Reply here if you need to change it.
   ```
4. **Wait**, name it "Wait: 2 hours before". An upcoming appointment or booking, Appointment / Calendar Event, **Before**, **2 hours**. If this date has already passed: **Skip all outbound communication actions till next wait or event start date action**.
5. **Send SMS**, message:
   ```
   See you at {{appointment.only_start_time}} today, {{contact.first_name}}. Harbor Physio Clinic
   ```

Merge fields used: `{{contact.first_name}}`, `{{appointment.start_time}}` (example "Wed, Nov 5, 2025 3:30 PM"), `{{appointment.only_start_time}}`. **Confirmed**, [list of merge fields](https://help.gohighlevel.com/support/solutions/articles/48001078171-list-of-merge-fields). The waits refer to the appointment that started the workflow: "When your workflow trigger is appointment-based, An upcoming appointment or booking shows an info banner referencing the trigger appointment" (**partial**: the page does not say more about which appointment is used).

What HighLevel does with this workflow on its own (**confirmed**, [appointment scenarios](https://help.gohighlevel.com/support/solutions/articles/155000002697-appointment-scenarios-in-workflow) and the trigger article):

- When staff mark the visit No Show, Cancelled or Invalid, "The customer will be pulled out of the workflow, and no further actions will occur." So a no-show gets no further reminders.
- "When you reschedule an appointment, it's treated as a new one. The contact will re-enter the workflow from the beginning."

Rebooked visits: n8n creates them with `toNotify: true`. The API docs say "If set to false, the automations will not run" (**confirmed**, [create appointment](https://marketplace.gohighlevel.com/docs/ghl/calendars/create-appointment/index.html) and the `toNotify` field in GoHighLevel's `calendars.json` OpenAPI file), so with `true` this workflow should start for the new visit and send the reminders. That an API-created appointment fires the Appointment Status trigger is **partial**: the trigger article says it "activates workflows when an appointment is scheduled or its status changes" and "Trigger fires when an appointment status is updated via API", and offers a "Modified By: API" filter, but never says so for an appointment created through the API. Book one test visit through the agent and check that it enters workflow 1.

## Workflow 2: Harbor Physio: no-show to rebooking agent

### Trigger: Appointment Status

| Setting | Value | Source |
|---|---|---|
| Trigger | **Appointment Status** | **Confirmed** |
| Filter: In Calendar | Your visits calendar | **Confirmed** |
| Filter: Filter By Appointment Status | **No-show** ("If the client does not attend") | **Confirmed**, the label is written "No-show" |
| Filter: Modified By | Leave empty (any), or **User** if only staff mark no-shows | **Confirmed** options: API, Customer, User, Workflow |

### Settings

| Setting | Value |
|---|---|
| Allow Re-entry | On (a patient can miss another visit months later) |
| Time Window | Off. n8n decides when the text may go out, in the patient's own time zone |

### Actions, in order

1. **Wait**, choose **A set period of time**, **15 minutes** (**confirmed**, [wait action](https://help.gohighlevel.com/support/solutions/articles/155000002470-action-wait-time-delay): "A set period of time" takes minutes, seconds, hours or days). This gives staff time to fix a status set by mistake, for example a patient who arrives late. n8n reads the appointment back after the wait and does nothing if it is no longer a no-show.
2. **Custom Webhook**: **+ Add action → Send Data → Custom Webhook** (**confirmed** action and menu path, [custom webhook](https://help.gohighlevel.com/support/solutions/articles/155000003305-workflow-action-custom-webhook)):

   | Field | Value |
   |---|---|
   | Event | **POST** ("Shows Body (Key and Value) pairs for a form-style payload; no Raw Body editor") |
   | URL | `https://YOUR-N8N-DOMAIN/webhook/ghl-no-show` |
   | Authorization | **Bearer Token**, token = the value of `GHL_WEBHOOK_SECRET` in n8n |
   | Headers | none |
   | Content-Type | Nothing to set. The article lists Content-Type only for Event CUSTOM and calls the POST body a form-style payload; n8n reads form fields and JSON alike |
   | Body | the key and value pairs below |

   | Key | Value |
   |---|---|
   | `event` | `no_show` |
   | `contactId` | `{{contact.id}}` |
   | `locationId` | `{{location.id}}` |

   n8n reads the body whether GoHighLevel sends it as JSON or as form fields; the test sends both. With Bearer Token, GoHighLevel sends `Authorization: Bearer <secret>`, and n8n hides that header in its saved runs (checked in the test). If the call carries the right secret but no contact id (for example a merge field typed wrong), n8n answers 200 and then fails the run, so the error workflow alerts you instead of the no-show being dropped quietly.

   There is no appointment ID in this body because HighLevel's merge field list has no `{{appointment.id}}` (**not confirmed** that one exists, so it is not used). n8n finds the visit itself: it lists the patient's appointments and takes the newest one marked no-show in the calendar, then reads that appointment back to confirm it.

   Custom Webhook is a premium action: "$0.01 per premium execution" after "100 free lifetime premium executions" per sub-account (**confirmed**, [premium features](https://help.gohighlevel.com/support/solutions/articles/155000005678-how-to-enable-and-rebill-premium-features-for-workflows)). Each no-show uses one execution.

### Alternative: the free Webhook action

The standard **Webhook** action cannot send headers (its article lists only Action Name, Method and URL, plus custom data). Point it at the same URL and, under **CUSTOM DATA**, click **+ Add item**: key `secret`, value = your `GHL_WEBHOOK_SECRET` (**confirmed**, the custom data section and its Key and Value fields are on the [webhook action](https://help.gohighlevel.com/support/solutions/articles/155000003299-workflow-action-webhook-outbound-) page).

For an appointment trigger its payload carries a `calendar` object with `appointmentId`, `startTime`, `status` and more (**confirmed** schema, [webhook action](https://help.gohighlevel.com/support/solutions/articles/155000003299-workflow-action-webhook-outbound-), which also spells one key `appoinmentStatus`). n8n then uses that appointment ID directly. Two details are **not confirmed** on the official page and come from community write-ups: the contact ID key (`contact_id`) and that custom data arrives inside a `customData` object. n8n accepts `contact_id`, `contactId` and `contact.id`, and the secret as `customData.secret` or a top-level `secret`. The test sends this payload shape too. Note that n8n saves the body of every run, so with this action the secret is stored in plain text in n8n's execution history; the Bearer Token of the Custom Webhook is not.

## Workflow 3: Harbor Physio: no-show replies to rebooking agent

### Trigger: Customer Replied

| Setting | Value | Source |
|---|---|---|
| Trigger | **Customer Replied** | **Confirmed**, [trigger article](https://help.gohighlevel.com/support/solutions/articles/155000002677-workflow-trigger-customer-replied) |
| Filter: Reply Channel | **SMS** | **Confirmed**: HighLevel's [SMS forwarding article](https://help.gohighlevel.com/support/solutions/articles/48001156789-incoming-sms-shows-up-in-conversations-but-isn-t-forwarded-to-the-forwarding-number-) builds the same trigger with "Reply Channel = SMS" |
| Filter: Has Tag | `no-show` | **Confirmed** filter name |

Other filters on the trigger, not used here: Contains Phrase, Exact Match Phrase, Doesn't Have Tag, Intent Type, Replied to Workflow (**confirmed**).

### Settings

| Setting | Value |
|---|---|
| Allow Re-entry | On (every reply must reach n8n). The same SMS forwarding article: "Without this setting enabled, a contact can only complete the workflow once" (**confirmed**) |
| Time Window | Off. n8n handles quiet hours itself: a reply that arrives at night is read and acted on at once (a picked time is booked), and the agent's text waits for the morning |

### Actions

1. **Custom Webhook** (**+ Add action → Send Data → Custom Webhook**):

   | Field | Value |
   |---|---|
   | Event | **POST** |
   | URL | `https://YOUR-N8N-DOMAIN/webhook/ghl-no-show-reply` |
   | Authorization | **Bearer Token**, token = `GHL_WEBHOOK_SECRET` |
   | Content-Type | Nothing to set (see workflow 2) |

   | Key | Value |
   |---|---|
   | `event` | `reply` |
   | `contactId` | `{{contact.id}}` |
   | `locationId` | `{{location.id}}` |
   | `body` | `{{message.body}}` |
   | `messageType` | `SMS` |
   | `direction` | `inbound` |

   `{{message.body}}` is a **confirmed** merge field ([list of merge fields](https://help.gohighlevel.com/support/solutions/articles/48001078171-list-of-merge-fields)), and in a Customer Replied workflow "It inserts the message content associated with the reply that triggered the workflow" (**confirmed**, the SMS forwarding article above; in the picker it is **Message → Message Body**). Key and value pairs let GoHighLevel encode the patient's text; a raw JSON body with `{{message.body}}` inside quotes would break on a reply that contains a double quote. The free Webhook action works here too (secret as Custom Data `secret`); its payload has a `message` object with `type`, `body`, `direction` and `status` (**confirmed** schema; that Customer Replied fills it is **partial**, the page says to run a test to see example data).

There is no message ID merge field, so n8n spots repeat deliveries by matching the reply to the newest message with the same words in the conversation (from the last 24 hours).

Turn off anything else that answers these patients automatically, such as a Conversation AI bot, or they get two replies.

### STOP replies

HighLevel sets DND by itself: "DND is enabled if a contact replies with opt-out keywords (e.g., STOP, UNSUBSCRIBE)" (**confirmed**, [DND article](https://help.gohighlevel.com/support/solutions/articles/48001214849-how-to-use-do-not-disturb-dnd-)). The reply still reaches n8n through workflow 3, which also sets SMS DND (keeping the other channels as they were), tags the patient `no-show-opted-out` and ends the conversation. The full keyword list and whether HighLevel's DND covers SMS only are **partial**.

## What n8n answers

| Situation | Response |
|---|---|
| Right secret, valid payload | `200 {"received": true}` at once; the work happens afterwards |
| Right secret, payload n8n ignores (another location, an email, an empty message) | `200 {"received": true, "ignored": "<reason>"}` |
| Right secret, but no contact id (a merge field that did not fill) | `200 {"received": true, "ignored": "no contact id in the payload"}`, then the run fails and the error workflow sends a Slack alert |
| Wrong or missing secret | `401 {"error": "unauthorized"}`, plus a Slack alert from the error workflow |
| A setting is missing or invalid in n8n (including a `GHL_WEBHOOK_SECRET` shorter than 16 characters or left at the example value) | `500` with the list of problems, plus a Slack alert |

Workflow history in GoHighLevel shows these status codes, which helps when a webhook step looks red.

## Open points to check in your sub-account

1. **Partial**: that an appointment created through the API (`POST /calendars/events/appointments` with `toNotify: true`) fires the Appointment Status trigger, so rebooked visits get workflow 1's reminders. The trigger article confirms status updates made through the API, not creation.
2. **Partial**: that selecting both New and Confirmed in workflow 1 cannot make a booking enter twice while it is still waiting in the workflow.
3. **Partial**: which appointment "An upcoming appointment or booking" waits for (the page only says it references the trigger appointment).
4. **Partial**: the exact If/Else operator for "contact tag includes". (The Reply Channel label **SMS** is now confirmed.)
5. **Not confirmed**: an `{{appointment.id}}` merge field. The merge field list has none, so the recipe does not rely on one.
6. **Not confirmed**: the contact ID key and the `customData` wrapper in the free Webhook action's payload (the page's example shows neither). n8n accepts the common variants, and a call it cannot read a contact id from fails loudly.
7. **Partial**: that Customer Replied fills the `message` object of the free Webhook action. (For the recommended Custom Webhook, `{{message.body}}` holding the reply is confirmed.)
8. **Partial**: the full list of STOP keywords HighLevel acts on, and whether its DND is SMS only.
9. **Partial**: the encoding of the Custom Webhook's POST body. HighLevel calls it "a form-style payload" and says to switch to Event CUSTOM "if your provider expects JSON", but does not name the content type. n8n reads form fields and JSON, and the test sends both.
10. **Not checked**: the menu path for calendar notification settings in your HighLevel version.
