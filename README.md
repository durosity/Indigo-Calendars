# Calendars & Reminders for Indigo

Brings Apple Calendar and Apple Reminders into [Indigo](https://www.indigodomo.com/).
Show upcoming events and due reminders on Indigo devices and control pages, fire
triggers when an event is about to start, and create events and reminders (and
complete them) from your automations.

- **Plugin ID:** `com.durosity.indigo.calendar`
- **Repository:** <https://github.com/Durosity/IndigoCalendar>
- **Current version:** 4.2.11

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Permissions](#permissions)
- [Devices](#devices)
- [Actions](#actions)
- [Triggers](#triggers)
- [Menu items](#menu-items)
- [Date and time formats](#date-and-time-formats)
- [Scripting API](#scripting-api)
- [Apple Events](#apple-events)
- [Known limitations](#known-limitations)
- [Migration from v1.x](#migration-from-v1x)
- [Version history](#version-history)

## Features

- **Calendar devices** that list your next events from Calendar.app, including every
  occurrence of recurring events.
- **Reminders devices** that list your reminders, grouped and ordered by due date.
- **Merged Calendar** devices that combine several calendar devices into one list.
- **ICS Calendar Feed** devices that read any iCalendar (`.ics`) URL.
- **Apple Events** device that watches the MacRumors feed and adds upcoming Apple
  events to a calendar of your choice.
- **Actions** to create events, create reminders, mark reminders complete, snooze
  reminders and set dates on undated reminders.
- **A trigger** that fires a set number of minutes before an event starts.
- **A scripting API** so other plugins and scripts can create a reminder, get its ID
  back, and complete that exact reminder later.

## Requirements

This plugin reads calendar and reminder data from Calendar.app and Reminders.app
on macOS. It tries Apple's modern EventKit framework first (fast, supports
recurring event expansion), and falls back to AppleScript automation when
EventKit access is unavailable.

## Installation

**Automatic (recommended):** The plugin includes a `requirements.txt` file. Indigo
will automatically install the PyObjC dependencies (`pyobjc-core`,
`pyobjc-framework-Cocoa`, `pyobjc-framework-EventKit`) when you install the plugin.

**Manual (if needed):**

```bash
pip3 install pyobjc-framework-EventKit pyobjc-framework-Cocoa
```

### Upgrading

Replace the plugin and reload it. Your devices, actions and triggers keep working.

If you are copying the plugin folder between Macs or sharing it, **delete the
`Contents/Packages` folder first**. Once Indigo has installed the dependencies, that
folder holds compiled libraries that macOS may refuse to open when they arrive
somewhere else ("Apple could not verify ... is free of malware"). With `Packages`
removed, Indigo downloads the dependencies again from `requirements.txt`.

## Permissions

When the plugin first starts, it issues a small AppleScript query to Calendar.app
and Reminders.app via the `/usr/bin/osascript` binary. macOS shows two
permission prompts:

- **Calendar access prompt**: click **OK**
- **Reminders access prompt**: click **OK**

After granting them, the permissions appear under
*System Settings > Privacy & Security > Calendars* and
*Privacy & Security > Reminders* (NOT just Automation, which is what would
happen if the plugin called AppleScript in-process). This matches the standard
location users expect.

### Why does the plugin invoke `osascript` rather than calling AppleScript in-process?

macOS attributes AppleEvents to the process that emits
them. If a Python plugin calls AppleScript in-process via `NSAppleScript`,
the AppleEvents are attributed to its host process (`IndigoPluginHost`),
which has no Calendar/Reminders usage description in its Info.plist, so
macOS routes the prompt through the Automation TCC channel instead.

If the same AppleScript runs via the `/usr/bin/osascript` binary in a
subprocess, the AppleEvents are attributed to `osascript`, which IS shipped
by Apple with the proper usage description keys. The user then sees a proper
Calendar / Reminders prompt, and the granted permission appears in the
expected panels.

This was discovered by digging through plugin v1.7.1 (the last AppleScript-only
release before the EventKit migration), which used `subprocess.run(['osascript', '-e', ...])`
and worked correctly. v2.x onward switched to in-process AppleScript and lost
this property. v4.2.6 restores the subprocess approach.

### Diagnosing permission problems

Two menu items are available under **Plugins > Calendars & Reminders**:

- **Check Calendar & Reminders Permissions...** logs the current state of
  every permission channel
- **Re-request Calendar & Reminders Access...** re-triggers the prompts
  without disabling/re-enabling the plugin

If the prompts won't appear again (e.g. they were dismissed or declined), reset
the relevant TCC entries from Terminal then disable and re-enable the plugin:

```bash
tccutil reset Calendar
tccutil reset Reminders
tccutil reset AppleEvents
```

(Resetting the system-wide entries rather than per-binary because `osascript`
itself doesn't have a separate per-app TCC entry under modern macOS.)

## Devices

Create devices from **Devices > New**, Type: **Calendars & Reminders**. All of them
are sensor-type devices, so they work in triggers, control pages and conditions.

| Device | What it does |
|---|---|
| **Calendar** | Lists upcoming events from one or more calendars in Calendar.app |
| **Reminders** | Lists reminders from one or more lists in Reminders.app |
| **Merged Calendar** | Combines several Calendar / ICS devices into a single list |
| **ICS Calendar Feed** | Reads events from an `.ics` URL |
| **Apple Events** | Adds upcoming Apple events from the MacRumors feed to a calendar (see [Apple Events](#apple-events)) |

### Calendar

| Setting | Notes |
|---|---|
| Calendars to Monitor | Pick one or more calendars. Use **Refresh Available Calendars** after adding a calendar in Calendar.app. |
| Number of Events to Import | How many events get states (default 7). |
| Days Ahead to Fetch | 7, 14, 30, 60 (default) or 90 days. |
| Remove Duplicate Events | On by default. |
| Filter Keywords (exclude events) | Events whose title contains one of these words are skipped. |
| Auto-Refresh Interval | Disabled, or 1, 5 (default), 10, 15, 30 minutes or 1 hour. |

**States:** `lastUpdate`, `eventCount`, and for each event slot `NN` (01, 02, ...):
`eventNNName`, `eventNNCalendarName`, `eventNNLocation`, `eventNNStartDate`,
`eventNNStartTime`, `eventNNEndDate`, `eventNNEndTime`, `eventNNDuration`,
`eventNNAllDay` and `eventNNCPdate` (a ready-to-display line for control pages).

### Reminders

| Setting | Notes |
|---|---|
| Lists to Monitor | Pick one or more reminder lists. Use **Refresh Available Lists** after adding a list. |
| Number of Reminders to Import | How many reminders get states (default 10). |
| Today / Tomorrow / Future / Yesterday / Older / Undated | A priority from 1 (highest) to 9 for each group, which controls the order reminders are listed in. |
| High Priority (!!) tasks show at top of day's list | Off by default. |
| Auto-Refresh Interval | As for Calendar. |

Only incomplete reminders are listed.

**States:** `lastUpdate`, `reminderCount`, and for each slot `NN`: `reminderNNName`,
`reminderNNDate`, `reminderNNCPdate` (ready-to-display line) and `reminderNNId`.

### Merged Calendar

Pick the Calendar / ICS devices to merge, how many events to display (default 10),
and whether to remove duplicates. It exposes the same event states as a Calendar
device and updates when its source devices do.

### ICS Calendar Feed

Enter the feed URL. Settings otherwise match a Calendar device (event count, days
ahead, duplicate removal, keyword filter, refresh interval). The feed is fetched over
HTTP(S), so it needs no Calendar permission.

## Actions

Add these from any trigger, schedule or action group (**Plugins > Calendars & Reminders**).

| Action | What it does |
|---|---|
| **Refresh Calendar Events** | Refresh a Calendar device now instead of waiting for its interval. |
| **Create Calendar Event** | Add an event to a calendar: name, all-day option, start/end date and time, location. |
| **Create Reminder** | Add a reminder to a list: name, optional due date and time, priority (None, High, Medium, Low) and notes. |
| **Mark Reminder as Complete** | Complete the reminder in a chosen slot of a Reminders device. |
| **Snooze Reminder** | Push a reminder's due date back by a number of days and/or hours. |
| **Set Date on Undated Reminders** | Give every undated reminder in the chosen lists a date, taken from a specific date, a number of days from now, or an Indigo variable. |
| **Check Apple Events Now** | Check the MacRumors feed immediately (Apple Events device). |

Names, notes, locations, dates and times can contain `%%v:VARIABLEID%%` to insert the
value of an Indigo variable.

## Triggers

**Calendar Event Starting** fires before an event on a Calendar device starts. Choose
the device, the event slot, how many minutes before the event to fire (0 means at the
start), and optionally an event name to match.

## Menu items

Under **Plugins > Calendars & Reminders**:

- **Check Calendar & Reminders Permissions...**
- **Re-request Calendar & Reminders Access...**

## Date and time formats

Anywhere an action asks for a date or time, you can use any of these.

**Dates**

| Format | Meaning |
|---|---|
| `2026-10-31` | That exact date |
| `+0`, `+1`, `+7` | Today, tomorrow, a week from now |
| `%%v:123456%%` | The value of Indigo variable 123456 (blank means no date) |

**Times**

| Format | Meaning |
|---|---|
| `09:30` | That exact time (24-hour) |
| `+2h`, `+30m`, `+2h30m` | That long from now |
| `%%v:123456%%` | The value of Indigo variable 123456 (blank means no time) |

An unrecognised value falls back to today (dates) or now (times).

## Scripting API

Other plugins and scripts can drive the plugin through `executeAction`. Since v4.2.11
the actions return a result, so a caller can create a reminder, keep its ID, and
complete that exact reminder later.

| Action ID | Props | Returns (`indigo.Dict`) |
|---|---|---|
| `createReminder` | `targetList`, `reminderName`, `hasDueDate`, `dueDate`, `dueTime`, `priority`, `notes` | `success`, plus `reminderId` and `list` on success or `error` on failure |
| `completeReminderById` | `reminderId` | `success`, `reminderId`, plus `alreadyComplete` or `notFound` where relevant |
| `getReminderLists` | none | an `indigo.List` of reminder list names |

`completeReminderById` and `getReminderLists` are hidden from the Indigo UI because
they're meant for scripts and other plugins. `priority` is `"0"` (none), `"1"` (high),
`"5"` (medium) or `"9"` (low).

```python
cal = indigo.server.getPlugin("com.durosity.indigo.calendar")

result = cal.executeAction("createReminder", props={
    "targetList": "Home",
    "reminderName": "Replace batteries: Hallway Motion",
    "hasDueDate": True,
    "dueDate": "+2",
    "priority": "1",
    "notes": "Battery level is 12%",
}, waitUntilDone=True)

if result and result.get("success"):
    reminder_id = result["reminderId"]
    # ...later, when the job is done:
    cal.executeAction("completeReminderById",
                      props={"reminderId": reminder_id}, waitUntilDone=True)
```

Reminder IDs are EventKit identifiers from the Mac running Indigo. If a reminder was
deleted, `completeReminderById` returns `notFound` rather than an error.

**Used by:** [BatteryWatch](https://github.com/Durosity/Indigo-BatteryWatch), which adds
a reminder when a battery is low or a device stops responding, and completes it when
the battery is replaced.

## Apple Events

Create a device of type **Apple Events**, pick the calendar to write to (for
example *Mike Personal*), and save. The device checks the feed hourly by default
(30 minutes to 4 hours, or manual only) and also has a **Check Apple Events Now**
action and responds to *Send Status Request*.

There are two kinds of entry:

| | Rumoured | Confirmed |
|---|---|---|
| Source | A MacRumors story reporting a date ("reportedly plans to unveil ... on October 13") | A MacRumors story reporting Apple's own announcement ("Apple Event Announced for September 9", "Apple Product Launch Announced for October 13") |
| Calendar entry | All-day, titled with your label, notes say it is unconfirmed | Timed 1-hour event if the story states a time with a timezone; otherwise all-day, with a note that the start time isn't known yet |
| Optional? | Yes: **Include Rumoured Events** (off by default) | Always added |

**Labels.** With *Include Rumoured Events* ticked you can set a **Label Before
Name** and/or **Label After Name**. With the event name `Apple Event`:

| Before | After | Result |
|---|---|---|
| `[Rumour]` | | `[Rumour] Apple Event` |
| | `(Rumoured)` | `Apple Event (Rumoured)` |
| `[Rumour]` | `(Rumoured)` | `[Rumour] Apple Event (Rumoured)` |

At least one label is required, so a rumour can't be mistaken for a confirmed date.

**When Apple confirms a rumoured date**, the existing entry is upgraded in place
(same calendar item): the label is dropped and the title becomes e.g.
`Apple Event: Surprise and Shine`. It becomes a timed event only if a start time with
a timezone was announced. Anything you typed in the notes is kept. If the confirmed
date differs from a rumoured one, both are kept, so delete the stale rumour yourself.

**Deleting is respected.** If you delete a rumoured entry from Calendar.app the
plugin will not add it back. A *confirmed* announcement is new information, so it is
still added.

**How rumours are recognised.** A story must (a) have a launch-type keyword in the
headline (*Rumour Headline Keywords*), (b) not contain an ignore word (*Ignore
Headlines Containing*), (c) read like a rumour (words such as "reportedly",
"plans to", "according to", "Gurman"), and (d) give a concrete day. If the day is
only in the story text rather than the headline, it must be in the opening lines
with launch wording right beside it. Month-only reports ("in October or November")
are ignored. Rumoured dates are only added within *Rumour Look-Ahead*; official
announcements are accepted up to a year ahead.

**Device states:** `status`, `lastResult`, `lastUpdate`, `upcomingCount`,
`nextEventName`, `nextEventDate`, `nextEventKind` (confirmed/rumoured) and
`nextEventDaysAway` (-1 when none), usable in triggers and control pages.

**Limits to know about**

- The MacRumors feed holds roughly the last 40 hours of stories, so Indigo needs to
  be running at least every day or so to see everything.
- It relies on story headlines, so it is a heuristic. Adjust the keyword lists if
  something is missed or wrongly caught. Turn on *Enable Debug Logging* to see
  every candidate and why it was chosen.
- What has been added is remembered in
  `Preferences/Plugins/com.durosity.indigo.calendar/appleEvents_<deviceID>.json`
  inside your Indigo folder. Delete that file to make the device forget everything
  (existing calendar entries are not removed).
- Events are marked as *free* so they don't block time on shared calendars.

## Known limitations

- **Tags not supported.** Neither Calendar event tags nor Reminder tags are accessible
  through EventKit or any Apple API.
- **Recurring event expansion only on the EventKit path.** When the plugin is
  using the AppleScript fallback, recurring events are NOT automatically expanded
  into individual occurrences. This is a Calendar.app AppleScript limitation.

## Migration from v1.x

No configuration changes needed. Your existing calendar and reminder list
selections will work as-is.

## Version history

### v4.2.11

- **Scripting API for other plugins.** `createReminder` now returns an `indigo.Dict`
  (`success`, plus `reminderId` and `list` on success or `error` on failure), and two
  new script-only actions (hidden from the Indigo UI) let a calling plugin work with
  that ID:
  - `completeReminderById` - props: `reminderId`. Returns `success` and `reminderId`,
    plus `alreadyComplete` or `notFound` where relevant.
  - `getReminderLists` - no props. Returns an `indigo.List` of reminder list names.

  Call them with `plugin.executeAction(actionId, props={...}, waitUntilDone=True)`.
  Used by the new BatteryWatch plugin. Existing actions and devices are unchanged.

### v4.2.10

- **Fixed: Apple Events missed Apple's own "product launch" announcements.** The
  confirmed-event check only recognised the words *event*, *keynote* and *WWDC*.
  When MacRumors reported "Apple Product Launch Announced for October 13", the story
  was treated as a rumour and the existing rumoured entry was never upgraded. It
  now also recognises *launch*, *presentation* and *announcement* wording, and
  treats "Apple today announced..." in a story's opening sentence as a backup signal.
- **Start times are no longer guessed.** A confirmed entry is only timed when the
  story gives a time **with a timezone** ("10:00 a.m. Pacific Time"). Otherwise it is
  a confirmed all-day entry whose notes say the start time isn't known yet. Previously
  a missing time defaulted to 10:00 Pacific, and a bare "9:00 a.m." was assumed to be
  Pacific too.

### v4.2.9

- **New device type: Apple Events.** Watches the MacRumors feed and adds upcoming
  Apple events (keynotes, launches) to a calendar of your choice in Calendar.app.
  See [Apple Events](#apple-events).
- **Fixed `Events.xml`.** The "Calendar Event Starting" trigger used the short
  `<n>` name tag where every other XML file uses `<Name>`.

### v4.2.8

- **Fixed "Set Date on Undated Reminders" action.** It was calling
  `reminder.completed()` which doesn't exist on `EKReminder`. The correct
  accessor is `isCompleted()` (Objective-C BOOL getter convention, which
  PyObjC exposes verbatim rather than synthesising a `completed()` wrapper).
- **Fixed EventKit save/remove error handling** in five places. PyObjC
  translates Objective-C `NSError**` out-parameters into an extra tuple
  element on the return value, so `saveEvent:span:commit:error:` and
  `saveReminder:commit:error:` actually return `(success, error)`. The
  previous code was passing `None` as the error and reading back a single
  return value, which meant `success` was always a truthy tuple, so save
  failures were silently treated as successes, and any returned error never
  reached the log. Affects: Create Calendar Event, Create Reminder, Mark
  Reminder Complete, Snooze Reminder, and Set Date on Undated Reminders.

### v4.2.7

- **Removed the bundled `Contents/Packages/` directory.** Previous releases
  shipped a 6.6 MB copy of PyObjC inside the plugin bundle alongside a
  `requirements.txt` declaring the same packages, so Indigo was installing
  them twice. PyObjC is now sourced exclusively via `requirements.txt`, which
  Indigo installs into its Python environment on plugin install. The bundle
  is now 208 KB instead of 6.9 MB.
- **`requirements.txt` now declares all three packages explicitly**
  (`pyobjc-core`, `pyobjc-framework-Cocoa`, `pyobjc-framework-EventKit`)
  rather than relying on `pyobjc-core` being pulled in transitively.

### v4.2.6

- **Routes AppleScript invocations through `osascript`** instead of in-process
  `NSAppleScript`. This fixes a regression introduced when the plugin migrated
  to EventKit in v2.x: the in-process approach causes prompts to fire under
  the *Automation* TCC panel rather than under *Calendars* / *Reminders*. The
  subprocess approach mirrors what v1.7.1 did and produces prompts in the
  expected panels.

### v4.2.5

- **AppleScript fallback for the read path.** When the EventKit framework
  refuses access, the plugin now reads calendars, events, reminder lists, and
  reminders via AppleScript instead.
- **Hybrid execution model.** Both paths attempt every read. EventKit is
  preferred when available; AppleScript is used as a transparent fallback.
- **Honest log output.** Startup reports which path is working.

### v4.2.4

- **AppleScript-based permission bootstrap.** Pokes Calendar.app and Reminders.app
  via AppleScript at startup to trigger user-visible permission prompts.
- **New menu items.** "Check Permissions" and "Re-request Access".

### v4.2.3

- **Fixed permission flow on macOS 14+ (Sonoma) and macOS 26 (Tahoe).** Uses
  the modern `requestFullAccessToEvents:` / `requestFullAccessToReminders:` APIs.
- **Single shared `EKEventStore`**.
- **Removed duplicate empty `shutdown()`**.

### v2.1.0

- **Reminders use EventKit.** Replaced AppleScript with native API.
- **Much faster reminder fetching.** No more AppleScript overhead or timeouts.
- **Consistent API.** Both calendars and reminders use the same backend.

### v2.0.x

- **Recurring events support.** Now properly fetches all instances of recurring events.
- **EventKit backend.** Replaced AppleScript with Apple's native EventKit API.
