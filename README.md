# Calendars & Reminders for Indigo

An [Indigo](https://www.indigodomo.com) plugin that brings macOS **Calendar** and **Reminders** into your home automation. It lets you show upcoming events and reminders on control pages, fire triggers before events start, create events and reminders from actions, and tick reminders off automatically when something happens in your house.

It uses Apple's EventKit framework for fast, native access, including proper expansion of recurring events. If EventKit access isn't available, it falls back to AppleScript automatically.

---

## Features

- **Calendar devices** show the next events from any calendars in Calendar.app (iCloud, Google, Exchange, local), with names, times, locations and durations as device states.
- **Reminders devices** show your next reminders from chosen lists, with a configurable sort order by due date and priority.
- **ICS Calendar Feed devices** read any public `.ics` URL, so you don't have to add it to Calendar.app.
- **Merged Calendar devices** combine several calendar devices into one sorted, de-duplicated list.
- **Apple Events devices** watch MacRumors and add upcoming Apple keynotes and launches to a calendar of your choice.
- **Actions** let you create events and reminders, complete, snooze or date reminders, and refresh devices on demand.
- **Complete Reminder from Variable** creates a reminder in one trigger and ticks it off from another, for example when someone arrives at a geofence.
- **Calendar Event Starting trigger** fires at, or a set number of minutes before, an event's start, optionally only for events matching some text.
- **Keyword filtering** excludes events you don't care about, such as bank holidays in other UK nations.
- **Flexible dates and times** in actions: `+0`, `+1`, `+6h`, `+2h30m`, `HH:MM`, and `%%v:ID%%` variable substitution.
- **Scripting API** gives other plugins and Python scripts reminder IDs back from Create Reminder and lets them complete reminders by ID.

---

## Requirements

- Indigo 2022.1 or later (Server API 3.0)
- macOS with Calendar and Reminders. This is tested on macOS 14 Sonoma and later, including macOS 26 Tahoe.
- PyObjC (`pyobjc-core`, `pyobjc-framework-Cocoa`, `pyobjc-framework-EventKit`). Indigo installs these automatically from `requirements.txt`.

---

## Installation

1. Download the latest release, or install from the Indigo Plugin Store.
2. Double-click `Calendars & Reminders.indigoPlugin` to install it into Indigo.
3. Indigo installs the Python dependencies from `requirements.txt` on first launch.

If the dependencies don't install for any reason, you can install them manually:

```bash
pip3 install pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-EventKit
```

### Granting access

On first start, macOS shows two prompts, one for **Calendars** and one for **Reminders**. Click **OK** on both. The permissions then appear under *System Settings → Privacy & Security → Calendars* and *→ Reminders*.

If you miss or decline the prompts, see [Troubleshooting](#troubleshooting).

---

## Device Types

### Calendar

This device monitors one or more calendars from Calendar.app.

| Setting | Description |
|---|---|
| Calendars to Monitor | Pick any number of calendars. Use **Refresh Available Calendars** if a new one is missing. |
| Number of Events to Import | How many upcoming events become device states (slots `01`, `02`, …). |
| Days Ahead to Fetch | 7, 14, 30, 60 or 90 days. |
| Remove Duplicate Events | Hides identical events that appear on more than one calendar. |
| Filter Keywords | Comma-separated words. Events containing any of them are excluded, e.g. `Scotland, Wales, Northern Ireland`. |
| Auto-Refresh Interval | Disabled, or 1 minute to 1 hour. |

**States per slot** (`NN` = `01`, `02`, …):
`eventNNName`, `eventNNCalendarName`, `eventNNLocation`, `eventNNStartDate`, `eventNNStartTime`, `eventNNEndDate`, `eventNNEndTime`, `eventNNDuration`, `eventNNAllDay`, and `eventNNCPdate`. The last one is a ready-formatted string for control pages.

Device-level states are `eventCount` and `lastUpdate`.

### Reminders

This device monitors one or more reminder lists.

| Setting | Description |
|---|---|
| Lists to Monitor | Pick any number of Reminders lists. |
| Number of Reminders to Import | How many reminders become device states. |
| Priority Order | Rank *Today*, *Tomorrow*, *Future*, *Yesterday*, *Older* and *Undated* reminders from 1 (top) to 9 to control the sort order. |
| High Priority at Top | Puts `!!` reminders at the top of their day's group. |
| Auto-Refresh Interval | Disabled, or 1 minute to 1 hour. |

**States per slot:** `reminderNNName`, `reminderNNDate`, `reminderNNCPdate`, `reminderNNId`.

Device-level states are `reminderCount` and `lastUpdate`.

### ICS Calendar Feed

This device has the same event states and options as a Calendar device, but reads from an `.ics` URL rather than Calendar.app. It suits public sports fixtures, school term dates, bin collections and similar feeds.

### Merged Calendar

This device combines any number of Calendar and ICS devices into a single sorted list, with optional de-duplication. It's useful when one control page needs to show "what's next" across everything.

### Apple Events

This device watches the MacRumors feed and adds upcoming Apple events to a calendar in Calendar.app.

- **Confirmed events** are those Apple has announced. They are always added, as a timed event if the story gives a time with a timezone, otherwise as all-day.
- **Rumoured events** are optional and off by default. They are added as all-day entries with a label you choose, such as `[Rumour] Apple Event`, so they can't be mistaken for confirmed dates.
- When Apple confirms a rumoured date, the existing entry is **upgraded in place**, and anything you've added to its notes is kept.
- If you delete a rumoured entry, the plugin won't add it back.

The states are `status`, `lastResult`, `lastUpdate`, `upcomingCount`, `nextEventName`, `nextEventDate`, `nextEventKind` (`confirmed` or `rumoured`) and `nextEventDaysAway` (`-1` when none).

Things to bear in mind:
- The MacRumors feed only holds about 40 hours of stories, so Indigo needs to be running regularly to catch everything.
- Detection is headline-based, so it is a heuristic. The keyword lists can be tuned under the device's advanced settings, and debug logging shows why each story was or wasn't picked up.
- What has been added is remembered in `Preferences/Plugins/com.durosity.indigo.calendar/appleEvents_<deviceID>.json`. Delete that file to reset the device. Existing calendar entries are not removed.

---

## Actions

| Action | Description |
|---|---|
| **Create Calendar Event** | Adds an event to any calendar, with optional end, all-day and location. |
| **Create Reminder** | Adds a reminder to any list, with optional due date and time, priority and notes. It can optionally **save the new reminder's ID to a variable**. |
| **Complete Reminder from Variable** | Marks the reminder whose ID is stored in a variable as complete, and optionally clears the variable. |
| **Mark Reminder as Complete** | Completes the reminder currently in a given slot of a Reminders device. |
| **Snooze Reminder** | Pushes a slot's reminder back by a number of days and/or hours. |
| **Set Date on Undated Reminders** | Gives every undated reminder in the chosen lists a due date: a specific date and time, *N* days from now, or a value from a variable. |
| **Refresh Calendar Events** | Refreshes a Calendar device immediately. |
| **Check Apple Events Now** | Checks the feed for an Apple Events device immediately. |

All devices also respond to Indigo's **Send Status Request**, which triggers a refresh.

### Date and time formats

Text fields in the create actions accept:

| Input | Meaning |
|---|---|
| `2026-12-25` | A specific date |
| `+0`, `+1`, `+7` | Today, tomorrow, 7 days from now |
| `07:00` | A specific time (24-hour) |
| `+30m`, `+6h`, `+2h30m` | Relative to now |
| `%%v:12345%%` | The value of an Indigo variable |

Names, notes and locations also accept `%%v:ID%%` substitution.

---

## Triggers

### Calendar Event Starting

This trigger fires when an event on a Calendar, ICS or Merged device is about to start.

- **Event Slot:** a specific slot, or *Any Event*.
- **Minutes Before Event:** `0` fires at the start time.
- **Match Event Name:** optional, case-insensitive text match.

You can also trigger on any device state change in the usual Indigo way, for example when `reminderCount` changes.

---

## Example: complete a reminder automatically

This is the problem the variable-based actions were built for. Say you want a reminder to feed someone's cats at 7am if they stayed out overnight, which ticks itself off if they come home first.

1. Create a variable, e.g. `catReminderId`.
2. **Trigger A** runs at 2am, with a condition that the person is at the other house (using iFindFriendsMini, for example). It runs **Create Reminder** for "Feed the cats", due `+0` at `07:00`, with **Save Reminder ID** set to `catReminderId`.
3. **Trigger B** fires on *GeoFence Enter* for the person and their home. It runs **Complete Reminder from Variable** on `catReminderId`, with **Clear Variable** ticked.

Here's how the edge cases are handled:
- If the variable is empty, nothing happens. Only a debug message is logged, so Trigger B is safe to fire every time they come home.
- If the reminder was already ticked off by hand, or deleted, the variable is still cleared.
- If completion fails for another reason, such as missing Reminders access, the variable is left in place so a later run can retry.

---

## Menu Items

Under **Plugins → Calendars & Reminders**:

- **Check Calendar & Reminders Permissions…** logs the state of every permission channel.
- **Re-request Calendar & Reminders Access…** shows the macOS prompts again without restarting the plugin.

---

## Scripting API

Other plugins and Python scripts can call these actions with `executeAction(..., waitUntilDone=True)` and receive an `indigo.Dict` back.

```python
cr = indigo.server.getPlugin("com.durosity.indigo.calendar")

# Create a reminder and get its ID
result = cr.executeAction("createReminder", props={
    "targetList": "Reminders",
    "reminderName": "Feed the cats",
    "hasDueDate": True, "dueDate": "+1", "dueTime": "07:00",
    "priority": "0", "notes": ""
}, waitUntilDone=True)
# -> success, reminderId, list   (or success=False, error)

# Complete it later
cr.executeAction("completeReminderById",
                 props={"reminderId": result["reminderId"]},
                 waitUntilDone=True)
# -> success, reminderId, plus alreadyComplete / notFound / error where relevant

# List the available reminder lists
lists = cr.executeAction("getReminderLists", waitUntilDone=True)
```

`completeReminderById` and `getReminderLists` are hidden from the Indigo UI and exist for scripts and plugins only.

---

## Troubleshooting

**No permission prompts appeared, or I clicked "Don't Allow".**
Use **Re-request Calendar & Reminders Access…** from the plugin menu. If the prompts still don't appear, reset them from Terminal, then disable and re-enable the plugin:

```bash
tccutil reset Calendar
tccutil reset Reminders
tccutil reset AppleEvents
```

**Why does the plugin use `osascript` for its permission check?**
macOS attributes AppleEvents to the process that sends them. When they come from `/usr/bin/osascript`, which Apple ships with the correct usage descriptions, you get proper Calendars and Reminders prompts. Running AppleScript in-process from Indigo's plugin host would push the prompts into the less obvious *Automation* panel instead.

**Recurring events aren't expanding.**
That only happens on the AppleScript fallback path. Check the log at startup to see which path is in use, and grant Calendars access so EventKit is used.

**Something's not right.**
Turn on **Enable Debug Logging** in the device's settings and check the Indigo event log.

---

## Known Limitations

- **Tags** on events and reminders aren't available through EventKit or any other Apple API, so they can't be read.
- **Recurring event expansion** only works on the EventKit path.
- **Reminder IDs** are EventKit's `calendarItemIdentifier`. These are stable in normal use but can occasionally change after a full iCloud resync. If that happens, Complete Reminder from Variable logs a "not found" warning and clears the variable rather than failing silently.

---

## Changelog

### 4.2.13
- Version bump for Plugin Store packaging. No functional changes.

### 4.2.12
- New **Save Reminder ID** option on Create Reminder.
- New **Complete Reminder from Variable** action.

### 4.2.11
- Scripting API: `createReminder` returns the new reminder's ID. New hidden actions `completeReminderById` and `getReminderLists`.

### 4.2.10
- Apple Events recognises more "launch" and "announcement" wording and no longer guesses start times.

### 4.2.9
- New **Apple Events** device type.

### 4.2.8
- Fixed Set Date on Undated Reminders, and fixed EventKit save error handling across all write actions.

### 4.2.7
- Removed the bundled PyObjC copy. Dependencies now come from `requirements.txt` only.

### 4.2.3 – 4.2.6
- Modern macOS permission flow, an AppleScript fallback for reads, permission menu items, and `osascript`-based prompts.

### 2.x
- Moved to EventKit for calendars and reminders, with recurring event support.

---

## Author

Mike ([@durosity](https://github.com/durosity))

Issues and pull requests are welcome.
