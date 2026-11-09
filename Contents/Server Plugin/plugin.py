#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Calendar.py
# Calendar Plugin for Indigo
# Author: Durosity

import indigo
import os
import subprocess
from datetime import datetime, timedelta
import threading
import time

import apple_events

class Plugin(indigo.PluginBase):
    
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super(Plugin, self).__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.debug = pluginPrefs.get("debugLogging", False)
        self.refresh_threads = {}
        self.calendar_list_cache = None
        self.calendar_list_cache_time = None
        self.timeout_counts = {}  # Track consecutive timeouts per device
        self.last_timeout_warning = {}  # Track when we last warned about timeouts
        self.trigger_check_thread = None
        self.trigger_check_stop = False
        self.triggered_events = {}  # Track which events we've already triggered
        # EventKit access state - populated by request_eventkit_access() at startup
        self._ek_calendar_access = False
        self._ek_reminder_access = False
        # Shared EKEventStore - reused across calls to keep the TCC connection warm.
        # Creating a fresh store on every call was the source of the "stale connection"
        # warnings in 4.2.2 and earlier. EventKit explicitly recommends one store per app.
        self._ek_store = None
        # Serialises Apple Events checks (startup refresh, timer, manual action)
        self._apple_events_lock = threading.Lock()
        
    def startup(self):
        # First thing at startup: request access to Calendars and Reminders.
        # This MUST happen before any other EventKit calls or macOS will silently
        # return empty results instead of prompting the user.
        self.request_eventkit_access()
        
        # Start trigger checking thread
        self.trigger_check_stop = False
        self.trigger_check_thread = threading.Thread(target=self.check_event_triggers_loop)
        self.trigger_check_thread.daemon = True
        self.trigger_check_thread.start()
        
    def shutdown(self):
        # Stop trigger checking thread
        self.trigger_check_stop = True
        if self.trigger_check_thread:
            self.trigger_check_thread.join(timeout=2)
        
    def deviceStartComm(self, device):
        # Initialize lastUpdate state
        device.updateStateOnServer("lastUpdate", "Never")
        
        if device.deviceTypeId == "calendar":
            device.updateStateOnServer("eventCount", 0)
        elif device.deviceTypeId == "reminders":
            device.updateStateOnServer("reminderCount", 0)
        elif device.deviceTypeId == "mergedCalendar":
            device.updateStateOnServer("eventCount", 0)
        elif device.deviceTypeId == "icsCalendar":
            device.updateStateOnServer("eventCount", 0)
        elif device.deviceTypeId == "appleEvents":
            device.updateStateOnServer("status", "Starting")
        
        # Start refresh thread if auto-refresh is enabled (not for merged calendars)
        if device.deviceTypeId != "mergedCalendar":
            refresh_interval = int(device.pluginProps.get("refreshInterval", 0))
            if refresh_interval > 0:
                self.start_refresh_thread(device.id, refresh_interval)
        
        # Do initial refresh in background to avoid blocking
        if device.deviceTypeId == "calendar":
            thread = threading.Thread(target=self.refresh_calendar_events, args=(device,))
            thread.daemon = True
            thread.start()
        elif device.deviceTypeId == "reminders":
            # Test access first
            if self.test_reminders_access():
                thread = threading.Thread(target=self.refresh_reminders, args=(device,))
                thread.daemon = True
                thread.start()
            else:
                self.logger.error(f"{device.name}: Cannot access Reminders - check permissions")
        elif device.deviceTypeId == "mergedCalendar":
            thread = threading.Thread(target=self.refresh_merged_calendar, args=(device,))
            thread.daemon = True
            thread.start()
        elif device.deviceTypeId == "icsCalendar":
            thread = threading.Thread(target=self.refresh_ics_calendar, args=(device,))
            thread.daemon = True
            thread.start()
        elif device.deviceTypeId == "appleEvents":
            thread = threading.Thread(target=self.refresh_apple_events, args=(device,))
            thread.daemon = True
            thread.start()
        
    def deviceStopComm(self, device):
        # Stop refresh thread
        if device.id in self.refresh_threads:
            self.refresh_threads[device.id]["stop"] = True
    
    def deviceUpdated(self, origDev, newDev):
        """Called when a device is updated - trigger merged calendar refreshes if this is a source device"""
        # Only care about calendar and ICS calendar devices
        if origDev.deviceTypeId in ["calendar", "icsCalendar"]:
            # Find any merged calendar devices that use this as a source
            for merged_dev in indigo.devices.iter(filter="self.mergedCalendar"):
                source_ids = merged_dev.pluginProps.get("sourceDevices", [])
                if isinstance(source_ids, str):
                    source_ids = [source_ids] if source_ids else []
                
                # If this device is a source for the merged calendar, refresh it
                if str(origDev.id) in source_ids:
                    thread = threading.Thread(target=self.refresh_merged_calendar, args=(merged_dev,))
                    thread.daemon = True
                    thread.start()
            
    def log_debug(self, message, device=None):
        # Check device-specific debug setting if device provided
        if device:
            debug = device.pluginProps.get("debugLogging", False)
        else:
            debug = self.debug
            
        if debug:
            self.logger.info(f"[DEBUG] {message}")
    
    def filter_events(self, events, filter_keywords_str):
        """Filter out events based on comma-separated keywords (case-insensitive)"""
        if not filter_keywords_str or not filter_keywords_str.strip():
            return events
        
        # Parse keywords - strip whitespace and make lowercase
        keywords = [k.strip().lower() for k in filter_keywords_str.split(',') if k.strip()]
        
        if not keywords:
            return events
        
        # Filter events
        filtered_events = []
        for event in events:
            title_lower = event['title'].lower()
            # Check if any keyword is in the title
            if not any(keyword in title_lower for keyword in keywords):
                filtered_events.append(event)
        
        return filtered_events
    
    def substitute_variables(self, text):
        """Replace %%v:XXXXXX%% with variable values"""
        import re
        
        if not text:
            return text
        
        # Find all variable references
        pattern = r'%%v:(\d+)%%'
        matches = re.findall(pattern, text)
        
        for var_id in matches:
            try:
                variable = indigo.variables[int(var_id)]
                text = text.replace(f"%%v:{var_id}%%", variable.value)
            except:
                self.logger.error(f"Could not find variable with ID {var_id}")
        
        return text
    
    def parse_flexible_date(self, date_str, default_to_today=True):
        """Parse flexible date formats:
        - YYYY-MM-DD (exact date)
        - +N (N days from now, e.g., +0 for today, +1 for tomorrow)
        - %%v:XXXXX%% (variable substitution)
        - Empty/blank after variable substitution returns None (no date set)
        - Invalid format defaults to today if default_to_today=True
        Returns datetime object or None
        """
        from datetime import datetime, timedelta
        
        if not date_str:
            return None
        
        # Substitute variables first
        date_str = self.substitute_variables(date_str.strip())
        
        # If variable was empty/blank, return None (no date)
        if not date_str:
            return None
        
        # Handle +N format (days from now)
        if date_str.startswith('+'):
            try:
                days = int(date_str[1:])
                return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days)
            except:
                if default_to_today:
                    self.logger.warning(f"Invalid relative date format '{date_str}', defaulting to today")
                    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                else:
                    self.logger.error(f"Invalid relative date format: {date_str}")
                    return None
        else:
            # Try parsing as YYYY-MM-DD
            try:
                return datetime.strptime(date_str, '%Y-%m-%d')
            except:
                if default_to_today:
                    self.logger.warning(f"Invalid date format '{date_str}', defaulting to today")
                    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                else:
                    self.logger.error(f"Invalid date format: {date_str}")
                    return None
    
    def parse_flexible_time(self, time_str, default_to_now=True):
        """Parse flexible time formats:
        - HH:MM (exact time)
        - +Nh (N hours from now)
        - +Nm (N minutes from now)
        - +NhMm (N hours M minutes from now, e.g., +2h30m)
        - %%v:XXXXX%% (variable substitution)
        - Empty/blank after variable substitution returns None (no time set)
        - Invalid format defaults to current time if default_to_now=True
        Returns time as HH:MM string or None
        """
        from datetime import datetime, timedelta
        import re
        
        if not time_str:
            return None
        
        # Substitute variables first
        time_str = self.substitute_variables(time_str.strip())
        
        # If variable was empty/blank, return None (no time)
        if not time_str:
            return None
        
        # Handle relative times
        if time_str.startswith('+'):
            try:
                now = datetime.now()
                
                # Parse +NhMm format (e.g., +2h30m)
                hours_match = re.search(r'(\d+)h', time_str)
                mins_match = re.search(r'(\d+)m', time_str)
                
                hours = int(hours_match.group(1)) if hours_match else 0
                minutes = int(mins_match.group(1)) if mins_match else 0
                
                future_time = now + timedelta(hours=hours, minutes=minutes)
                return future_time.strftime('%H:%M')
            except:
                if default_to_now:
                    self.logger.warning(f"Invalid relative time format '{time_str}', defaulting to current time")
                    return datetime.now().strftime('%H:%M')
                else:
                    self.logger.error(f"Invalid relative time format: {time_str}")
                    return None
        else:
            # Try parsing as HH:MM
            try:
                datetime.strptime(time_str, '%H:%M')
                return time_str
            except:
                if default_to_now:
                    self.logger.warning(f"Invalid time format '{time_str}', defaulting to current time")
                    return datetime.now().strftime('%H:%M')
                else:
                    self.logger.error(f"Invalid time format: {time_str}")
                    return None
            
    def getDeviceStateList(self, device):
        """Dynamically generate state list based on event/reminder count"""
        state_list = indigo.PluginBase.getDeviceStateList(self, device)
        
        if device.deviceTypeId in ["calendar", "mergedCalendar", "icsCalendar"]:
            event_count = int(device.pluginProps.get("eventCount", 7))
            
            for i in range(1, event_count + 1):
                state_list.append({
                "Disabled": False,
                "Key": f"event{i:02d}Name",
                "StateLabel": f"Event {i:02d} Name",
                "TriggerLabel": f"Event {i:02d} Name",
                "ControlPageLabel": f"Event {i:02d} Name",
                "Type": 150
                })
                state_list.append({
                "Disabled": False,
                "Key": f"event{i:02d}CalendarName",
                "StateLabel": f"Event {i:02d} Calendar",
                "TriggerLabel": f"Event {i:02d} Calendar",
                "ControlPageLabel": f"Event {i:02d} Calendar",
                "Type": 150
                })
                state_list.append({
                "Disabled": False,
                "Key": f"event{i:02d}Location",
                "StateLabel": f"Event {i:02d} Location",
                "TriggerLabel": f"Event {i:02d} Location",
                "ControlPageLabel": f"Event {i:02d} Location",
                "Type": 150
                })
                state_list.append({
                "Disabled": False,
                "Key": f"event{i:02d}StartDate",
                "StateLabel": f"Event {i:02d} Start Date",
                "TriggerLabel": f"Event {i:02d} Start Date",
                "ControlPageLabel": f"Event {i:02d} Start Date",
                "Type": 150
                })
                state_list.append({
                "Disabled": False,
                "Key": f"event{i:02d}StartTime",
                "StateLabel": f"Event {i:02d} Start Time",
                "TriggerLabel": f"Event {i:02d} Start Time",
                "ControlPageLabel": f"Event {i:02d} Start Time",
                "Type": 150
                })
                state_list.append({
                "Disabled": False,
                "Key": f"event{i:02d}EndDate",
                "StateLabel": f"Event {i:02d} End Date",
                "TriggerLabel": f"Event {i:02d} End Date",
                "ControlPageLabel": f"Event {i:02d} End Date",
                "Type": 150
                })
                state_list.append({
                "Disabled": False,
                "Key": f"event{i:02d}EndTime",
                "StateLabel": f"Event {i:02d} End Time",
                "TriggerLabel": f"Event {i:02d} End Time",
                "ControlPageLabel": f"Event {i:02d} End Time",
                "Type": 150
                })
                state_list.append({
                "Disabled": False,
                "Key": f"event{i:02d}Duration",
                "StateLabel": f"Event {i:02d} Duration",
                "TriggerLabel": f"Event {i:02d} Duration",
                "ControlPageLabel": f"Event {i:02d} Duration",
                "Type": 150
                })
                state_list.append({
                "Disabled": False,
                "Key": f"event{i:02d}AllDay",
                "StateLabel": f"Event {i:02d} All Day",
                "TriggerLabel": f"Event {i:02d} All Day",
                "ControlPageLabel": f"Event {i:02d} All Day",
                "Type": 150
                })
                state_list.append({
                "Disabled": False,
                "Key": f"event{i:02d}CPdate",
                "StateLabel": f"Event {i:02d} Display",
                "TriggerLabel": f"Event {i:02d} Display",
                "ControlPageLabel": f"Event {i:02d} Display",
                "Type": 150
                })
        
        elif device.deviceTypeId == "reminders":
            reminder_count = int(device.pluginProps.get("reminderCount", 10))
            
            for i in range(1, reminder_count + 1):
                state_list.append({
                    "Disabled": False,
                    "Key": f"reminder{i:02d}Name",
                    "StateLabel": f"Reminder {i:02d} Name",
                    "Type": 150
                })
                state_list.append({
                    "Disabled": False,
                    "Key": f"reminder{i:02d}Date",
                    "StateLabel": f"Reminder {i:02d} Date",
                    "Type": 150
                })
                state_list.append({
                    "Disabled": False,
                    "Key": f"reminder{i:02d}CPdate",
                    "StateLabel": f"Reminder {i:02d} Display",
                    "Type": 150
                })
                state_list.append({
                    "Disabled": False,
                    "Key": f"reminder{i:02d}Id",
                    "StateLabel": f"Reminder {i:02d} ID",
                    "Type": 150
                })
        
        return state_list
        
    # ========================================
    # EventKit Access / Permissions
    # ========================================
    
    def _get_ek_store(self):
        """Return the shared EKEventStore, lazily creating it on first use.
        
        Apple's EventKit guidance: an app should keep ONE EKEventStore for its
        lifetime. Re-creating the store on every call (as v4.2.2 did) causes
        intermittent "no calendars returned" errors because TCC has to re-resolve
        the connection each time. We hold one store as an instance attribute and
        reuse it everywhere.
        """
        if self._ek_store is None:
            try:
                from EventKit import EKEventStore
                self._ek_store = EKEventStore.alloc().init()
                self.log_debug("Created shared EKEventStore")
            except ImportError:
                self.logger.error("PyObjC EventKit not available. Please install: "
                                  "pip3 install pyobjc-framework-EventKit pyobjc-framework-Cocoa")
                return None
            except Exception as e:
                self.logger.error(f"Failed to create EKEventStore: {e}")
                return None
        return self._ek_store
    
    def request_eventkit_access(self):
        """Request access to Calendars and Reminders at plugin startup.
        
        This is more nuanced than it looks. The EventKit
        requestFullAccessToEvents:/requestFullAccessToReminders: API has a
        hard requirement that the host process Info.plist contains
        NSCalendarsUsageDescription / NSCalendarsFullAccessUsageDescription /
        NSRemindersUsageDescription / NSRemindersFullAccessUsageDescription
        keys. IndigoPluginHost.app (the executable our code actually runs in)
        does NOT contain these keys, so the macOS 14+ requestFullAccess
        completion handler fires immediately with granted=False and no prompt
        is ever shown to the user.
        
        Our workaround is a two-step process:
        
        1. Issue a tiny AppleScript that talks to Calendar.app and Reminders.app.
           This triggers the AppleEvents (Automation) permission prompt, which
           does NOT require any usage description and reliably appears for the
           user. Once granted, IndigoPluginHost is registered as authorized to
           control those apps.
        
        2. Try the EventKit API anyway. On macOS 14+ this usually still
           fails because of the missing usage description, but the
           AppleScript approach above gives us a working fallback for the
           read path - we can use AppleScript to enumerate calendars and
           reminders if EventKit is unavailable.
        
        For users who want the EventKit path to work (faster, cleaner), they
        can manually patch IndigoPluginHost's Info.plist - but that's not
        something we can ask of an end user, and it gets clobbered on Indigo
        upgrades anyway. AppleScript is the durable solution.
        """
        try:
            from EventKit import (
                EKEventStore,
                EKEntityTypeEvent,
                EKEntityTypeReminder,
            )
            from Foundation import NSDate, NSRunLoop
        except ImportError:
            self.logger.error("PyObjC EventKit not available. Please install: "
                              "pip3 install pyobjc-framework-EventKit pyobjc-framework-Cocoa")
            return
        
        store = self._get_ek_store()
        if store is None:
            return
        
        # ---- Step 1: AppleScript poke to trigger AppleEvents (Automation) prompts ----
        # This is what reliably gets a permission UI in front of the user.
        self._poke_via_applescript()
        
        # ---- Step 2: Check EventKit auth status (it may have come along for the ride) ----
        try:
            cal_status = EKEventStore.authorizationStatusForEntityType_(EKEntityTypeEvent)
            rem_status = EKEventStore.authorizationStatusForEntityType_(EKEntityTypeReminder)
            self.log_debug(f"Post-AppleScript EventKit status: Calendars={cal_status}, Reminders={rem_status}")
        except Exception as e:
            self.log_debug(f"Could not read EventKit auth status: {e}")
            cal_status = -1
            rem_status = -1
        
        # 3=Authorized (legacy), 5=FullAccess (macOS 14+)
        self._ek_calendar_access = cal_status in (3, 5)
        self._ek_reminder_access = rem_status in (3, 5)
        
        # If EventKit didn't come along with AppleScript, try its own request
        # path. On macOS 14+ this typically returns "denied" instantly because
        # of the missing Info.plist keys, but on older macOS or systems where
        # the keys have been patched in this can still work.
        if not self._ek_calendar_access:
            self._ek_calendar_access = self._request_access_for_entity(
                store, EKEntityTypeEvent, "Calendars"
            )
        else:
            self.logger.info("Calendars: EventKit access already authorized")
        
        if not self._ek_reminder_access:
            self._ek_reminder_access = self._request_access_for_entity(
                store, EKEntityTypeReminder, "Reminders"
            )
        else:
            self.logger.info("Reminders: EventKit access already authorized")
        
        # Final summary - check whether either path (EventKit or AppleScript)
        # is functional. We test AppleScript reachability here rather than at
        # startup to give the user the most accurate picture in the log.
        as_cal_count = self._applescript_count_calendars()
        as_rem_count = self._applescript_count_reminder_lists()
        as_cal_ok = as_cal_count is not None
        as_rem_ok = as_rem_count is not None
        
        cal_ok = self._ek_calendar_access or as_cal_ok
        rem_ok = self._ek_reminder_access or as_rem_ok
        
        # Helper: describe which path is working
        def path_label(ek, as_):
            if ek and as_: return "EventKit + AppleScript"
            if ek: return "EventKit"
            if as_: return "AppleScript"
            return "none"
        
        if cal_ok and rem_ok:
            self.logger.info(f"Calendars accessible via: {path_label(self._ek_calendar_access, as_cal_ok)}")
            self.logger.info(f"Reminders accessible via: {path_label(self._ek_reminder_access, as_rem_ok)}")
        else:
            if not cal_ok:
                self.logger.error("Calendars: no working access path. Open System Settings > "
                                  "Privacy & Security > Automation > IndigoPluginHost and ensure "
                                  "Calendar is enabled.")
            if not rem_ok:
                self.logger.error("Reminders: no working access path. Open System Settings > "
                                  "Privacy & Security > Automation > IndigoPluginHost and ensure "
                                  "Reminders is enabled.")
            self.logger.error("If you don't see IndigoPluginHost listed under Automation, run "
                              "these in Terminal then disable/re-enable this plugin:")
            self.logger.error("  tccutil reset AppleEvents com.perceptiveautomation.IndigoPluginHost")
            self.logger.error("  tccutil reset Calendar com.perceptiveautomation.IndigoPluginHost")
            self.logger.error("  tccutil reset Reminders com.perceptiveautomation.IndigoPluginHost")
    
    def _poke_via_applescript(self):
        """Trigger TCC permission prompts for Calendar and Reminders by
        running tiny AppleScripts via the /usr/bin/osascript binary in a
        subprocess.
        
        WHY SUBPROCESS AND NOT NSAppleScript:
        
        When AppleScript code runs in-process via NSAppleScript, the
        AppleEvents it emits are attributed to the host process, which here
        is IndigoPluginHost. macOS routes the resulting permission prompt
        through the Automation TCC channel - the user sees an "IndigoPluginHost
        wants to control Calendar/Reminders" prompt, and the granted
        permission lives under Privacy & Security > Automation.
        
        When the same AppleScript code is run by the /usr/bin/osascript
        binary (Apple-shipped, code-signed by Apple, with all the right
        usage description keys baked into its embedded Info.plist), macOS
        attributes the AppleEvents to osascript, AND because osascript's
        Info.plist contains NSCalendarsUsageDescription and
        NSRemindersUsageDescription, the prompt that appears is the proper
        Calendar / Reminders prompt. The granted permission then lives under
        Privacy & Security > Calendars and Privacy & Security > Reminders -
        the panels users actually expect to look in.
        
        v1.7.1 of this plugin used subprocess+osascript and worked correctly
        for years. v2.x moved to NSAppleScript for performance, which broke
        the user-facing permission flow on fresh installs. v4.2.6 restores
        the subprocess approach for the permission-bootstrap step.
        """
        # Calendar.app poke
        try:
            self.log_debug("Poking Calendar.app via osascript to trigger permission prompt...")
            result = subprocess.run(
                ['osascript', '-e', 'tell application "Calendar" to count calendars'],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                self.log_debug(f"Calendar.app osascript poke succeeded "
                               f"(reports {result.stdout.strip()} calendars)")
            else:
                # rc != 0 typically means access was denied. The user will
                # have just seen (and dismissed) the prompt, OR the prompt
                # was previously declined. In either case we'll log a hint
                # but not an error; the final summary in
                # request_eventkit_access decides the overall verdict.
                stderr = (result.stderr or "").strip()
                if "1743" in stderr or "not authorized" in stderr.lower():
                    self.logger.warning(f"Calendar.app access was denied. "
                                        f"To grant: System Settings > Privacy & "
                                        f"Security > Calendars > enable osascript "
                                        f"(or any helper that invokes it).")
                else:
                    self.log_debug(f"Calendar.app osascript returned rc={result.returncode}: {stderr}")
        except subprocess.TimeoutExpired:
            self.logger.warning("Calendar.app osascript poke timed out - this usually means "
                                "a permission prompt is waiting for the user. Please answer it.")
        except Exception as e:
            self.log_debug(f"Calendar.app osascript exception: {e}")
        
        # Reminders.app poke
        try:
            self.log_debug("Poking Reminders.app via osascript to trigger permission prompt...")
            result = subprocess.run(
                ['osascript', '-e', 'tell application "Reminders" to count lists'],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                self.log_debug(f"Reminders.app osascript poke succeeded "
                               f"(reports {result.stdout.strip()} lists)")
            else:
                stderr = (result.stderr or "").strip()
                if "1743" in stderr or "not authorized" in stderr.lower():
                    self.logger.warning(f"Reminders.app access was denied. "
                                        f"To grant: System Settings > Privacy & "
                                        f"Security > Reminders > enable osascript "
                                        f"(or any helper that invokes it).")
                else:
                    self.log_debug(f"Reminders.app osascript returned rc={result.returncode}: {stderr}")
        except subprocess.TimeoutExpired:
            self.logger.warning("Reminders.app osascript poke timed out - this usually means "
                                "a permission prompt is waiting for the user. Please answer it.")
        except Exception as e:
            self.log_debug(f"Reminders.app osascript exception: {e}")
    
    def _request_access_for_entity(self, store, entity_type, label):
        """Request access for a single entity type (Event or Reminder).
        
        Tries the modern macOS 14+ API first, falls back to the legacy API.
        Uses an NSRunLoop spin to wait for the async completion handler since
        Indigo's plugin host does not run a Cocoa main run loop in the worker
        thread context.
        """
        from Foundation import NSDate, NSRunLoop
        
        # Check current authorization status BEFORE asking
        # 0=NotDetermined, 1=Restricted, 2=Denied, 3=Authorized (legacy "full access"),
        # 4=WriteOnly (events, macOS 14+), 5=FullAccess (macOS 14+)
        try:
            from EventKit import EKEventStore
            current_status = EKEventStore.authorizationStatusForEntityType_(entity_type)
            self.log_debug(f"{label} authorization status before request: {current_status}")
        except Exception:
            current_status = -1
        
        # Already authorized? (3 = legacy authorized, 5 = macOS 14+ full access)
        if current_status in (3, 5):
            self.logger.info(f"{label} access already granted (status={current_status})")
            return True
        
        # Denied or restricted - we cannot prompt again, user must fix in System Settings
        if current_status in (1, 2):
            self.logger.error(f"{label} access is denied or restricted (status={current_status}). "
                              f"Open System Settings > Privacy & Security > {label} and enable "
                              f"IndigoPluginHost.")
            return False
        
        # status == 0 (NotDetermined) or unknown - request access
        result = {"granted": False, "done": False, "error": None}
        
        def completion_handler(granted, error):
            result["granted"] = bool(granted)
            result["error"] = error
            result["done"] = True
        
        # Try the modern macOS 14+ API first
        used_modern_api = False
        try:
            if entity_type == 0:  # EKEntityTypeEvent
                if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
                    self.log_debug(f"Requesting {label} access via "
                                   f"requestFullAccessToEventsWithCompletion: (macOS 14+ API)")
                    store.requestFullAccessToEventsWithCompletion_(completion_handler)
                    used_modern_api = True
            else:  # EKEntityTypeReminder
                if hasattr(store, "requestFullAccessToRemindersWithCompletion_"):
                    self.log_debug(f"Requesting {label} access via "
                                   f"requestFullAccessToRemindersWithCompletion: (macOS 14+ API)")
                    store.requestFullAccessToRemindersWithCompletion_(completion_handler)
                    used_modern_api = True
        except Exception as e:
            self.log_debug(f"Modern API for {label} threw {e!r} - will try legacy API")
            used_modern_api = False
        
        # Fall back to the legacy API if the modern one isn't available
        if not used_modern_api:
            try:
                self.log_debug(f"Requesting {label} access via "
                               f"requestAccessToEntityType:completion: (legacy API)")
                store.requestAccessToEntityType_completion_(entity_type, completion_handler)
            except Exception as e:
                self.logger.error(f"Could not request {label} access: {e}")
                return False
        
        # Spin a run loop briefly to let the completion handler fire.
        # The TCC prompt is shown by the system; the handler runs once the user
        # answers (or immediately, if the answer is cached).
        deadline = time.time() + 15.0  # Up to 15s for user to click the prompt
        while not result["done"] and time.time() < deadline:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.1)
            )
        
        if not result["done"]:
            self.logger.warning(f"{label} access request did not complete within 15 seconds. "
                                f"If a system prompt appeared, please answer it; if not, check "
                                f"System Settings > Privacy & Security > {label}.")
            # Re-check status one more time in case the handler was just slow
            try:
                from EventKit import EKEventStore
                final_status = EKEventStore.authorizationStatusForEntityType_(entity_type)
                if final_status in (3, 5):
                    self.logger.info(f"{label} access is now granted (status={final_status})")
                    return True
            except Exception:
                pass
            return False
        
        if result["granted"]:
            self.logger.info(f"{label} access granted via EventKit")
            return True
        else:
            err = result["error"]
            err_msg = f": {err}" if err else ""
            # The most likely cause on macOS 14+ is that IndigoPluginHost.app's
            # Info.plist lacks NSCalendarsUsageDescription / NSRemindersUsageDescription
            # keys. macOS will instantly deny without prompting in that case.
            # A summary error is logged at the end of request_eventkit_access; here
            # we just log debug detail since the message is otherwise repetitive.
            self.log_debug(f"{label} EventKit request returned denied{err_msg} "
                           f"(this is expected on macOS 14+ if IndigoPluginHost "
                           f"lacks usage description keys; the AppleScript path "
                           f"should still work)")
            return False
    
    def _ensure_access(self, entity_type, label):
        """Re-check access on demand. Used as a guard before EventKit calls so
        that if the user grants access via System Settings while the plugin is
        running, we'll pick it up on the next refresh without needing a restart.
        """
        try:
            from EventKit import EKEventStore
            status = EKEventStore.authorizationStatusForEntityType_(entity_type)
            if status in (3, 5):
                return True
        except Exception:
            pass
        return False
    
    # ========================================
    # Menu Item Callbacks
    # ========================================
    
    def menu_check_permissions(self):
        """Diagnostic menu item: report the current permission state for both
        Calendars and Reminders so the user can see exactly where the issue is.
        """
        try:
            from EventKit import (
                EKEventStore,
                EKEntityTypeEvent,
                EKEntityTypeReminder,
            )
            
            self.logger.info("=== Permission Status ===")
            
            # EventKit auth status
            try:
                cal_status = EKEventStore.authorizationStatusForEntityType_(EKEntityTypeEvent)
                rem_status = EKEventStore.authorizationStatusForEntityType_(EKEntityTypeReminder)
                self.logger.info(f"EventKit Calendar status: {self._auth_status_name(cal_status)} ({cal_status})")
                self.logger.info(f"EventKit Reminders status: {self._auth_status_name(rem_status)} ({rem_status})")
            except Exception as e:
                self.logger.error(f"Could not read EventKit status: {e}")
            
            # AppleScript reachability (separate TCC channel)
            self.logger.info("Testing AppleScript access to Calendar.app...")
            cal_count = self._applescript_count_calendars()
            if cal_count is not None:
                self.logger.info(f"  AppleScript can see {cal_count} calendars in Calendar.app")
            else:
                self.logger.warning("  AppleScript cannot access Calendar.app - check System Settings > "
                                    "Privacy & Security > Automation > IndigoPluginHost")
            
            self.logger.info("Testing AppleScript access to Reminders.app...")
            rem_count = self._applescript_count_reminder_lists()
            if rem_count is not None:
                self.logger.info(f"  AppleScript can see {rem_count} reminder lists in Reminders.app")
            else:
                self.logger.warning("  AppleScript cannot access Reminders.app - check System Settings > "
                                    "Privacy & Security > Automation > IndigoPluginHost")
            
            self.logger.info("=== End Permission Status ===")
        except Exception as e:
            self.logger.error(f"Error checking permissions: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
    
    def menu_request_permissions(self):
        """Re-trigger the permission flow without needing to disable/re-enable
        the plugin. Useful if the prompt was missed or accidentally declined.
        """
        self.logger.info("Re-requesting Calendar and Reminders access...")
        self.request_eventkit_access()
        # Invalidate cached calendar list so the next config UI fetch tries fresh
        self.calendar_list_cache = None
        self.calendar_list_cache_time = None
    
    @staticmethod
    def _auth_status_name(status):
        """Map an EKAuthorizationStatus integer to a human-readable name."""
        names = {
            0: "NotDetermined",
            1: "Restricted",
            2: "Denied",
            3: "Authorized (legacy)",
            4: "WriteOnly (macOS 14+)",
            5: "FullAccess (macOS 14+)",
        }
        return names.get(status, "Unknown")
    
    def _applescript_count_calendars(self):
        """Return the number of calendars Calendar.app reports, or None on failure.
        Uses osascript subprocess so the call is attributed to osascript (not
        IndigoPluginHost) for TCC purposes - see _poke_via_applescript for
        the full explanation."""
        try:
            result = subprocess.run(
                ['osascript', '-e', 'tell application "Calendar" to count calendars'],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return None
            out = (result.stdout or "").strip()
            if not out:
                return None
            return int(out)
        except Exception:
            return None
    
    def _applescript_count_reminder_lists(self):
        """Return the number of lists Reminders.app reports, or None on failure."""
        try:
            result = subprocess.run(
                ['osascript', '-e', 'tell application "Reminders" to count lists'],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return None
            out = (result.stdout or "").strip()
            if not out:
                return None
            return int(out)
        except Exception:
            return None
    
    # ========================================
    # AppleScript Read Path (fallback when EventKit denied)
    # ========================================
    #
    # On macOS 14+, the EventKit framework refuses access for processes whose
    # Info.plist lacks NSCalendarsFullAccessUsageDescription /
    # NSRemindersFullAccessUsageDescription. IndigoPluginHost.app does not
    # contain those keys and we can't reasonably modify it.
    #
    # We invoke AppleScript via the /usr/bin/osascript binary in a subprocess
    # so that AppleEvents are attributed to osascript (which IS shipped by
    # Apple with the right Info.plist usage description keys) rather than to
    # IndigoPluginHost. The first time this happens after a fresh install,
    # macOS shows a proper Calendar / Reminders permission prompt and the
    # granted permission lives under Privacy & Security > Calendars and
    # Privacy & Security > Reminders (not just Automation).
    #
    # The shape of the returned data matches exactly what the EventKit path
    # produces, so the rest of the plugin doesn't need to know which path
    # was used.
    
    # Field/record separators chosen so they will not appear in calendar data.
    # These are ASCII control codes: 0x1E (record), 0x1F (field).
    _AS_REC_SEP = "\x1e"
    _AS_FLD_SEP = "\x1f"
    
    def _run_applescript(self, source, timeout_seconds=60):
        """Execute an AppleScript via the osascript subprocess and return its
        stdout as a string, or None on failure.
        
        Going through /usr/bin/osascript (rather than NSAppleScript in-process)
        is critical for TCC permissioning - see the section header above.
        """
        try:
            result = subprocess.run(
                ['osascript', '-e', source],
                capture_output=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout_seconds,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                if "1743" in stderr or "not authorized" in stderr.lower():
                    self.logger.error(f"AppleScript denied: {stderr}. "
                                      f"Check System Settings > Privacy & Security > "
                                      f"Calendars and Reminders.")
                else:
                    self.log_debug(f"osascript rc={result.returncode}: {stderr}")
                return None
            return result.stdout if result.stdout is not None else ""
        except subprocess.TimeoutExpired:
            self.logger.error(f"AppleScript timed out after {timeout_seconds}s")
            return None
        except Exception as e:
            self.logger.error(f"osascript exception: {e}")
            return None
    
    def _applescript_get_calendar_titles(self):
        """Return a list of calendar titles via Calendar.app, or None on failure."""
        # Build a single-line list separated by our record separator. AppleScript's
        # default list-as-string uses ", " which can collide with user titles
        # containing commas, so we set our own delimiter.
        rs = self._AS_REC_SEP
        source = (
            f'set AppleScript\'s text item delimiters to "{rs}"\n'
            f'tell application "Calendar"\n'
            f'    set t to title of every calendar\n'
            f'end tell\n'
            f'set out to t as string\n'
            f'set AppleScript\'s text item delimiters to ""\n'
            f'return out'
        )
        result = self._run_applescript(source)
        if result is None:
            return None
        if result == "":
            return []
        return [t for t in result.split(rs) if t]
    
    def _applescript_get_reminder_list_titles(self):
        """Return a list of reminder list names via Reminders.app, or None on failure."""
        rs = self._AS_REC_SEP
        source = (
            f'set AppleScript\'s text item delimiters to "{rs}"\n'
            f'tell application "Reminders"\n'
            f'    set t to name of every list\n'
            f'end tell\n'
            f'set out to t as string\n'
            f'set AppleScript\'s text item delimiters to ""\n'
            f'return out'
        )
        result = self._run_applescript(source)
        if result is None:
            return None
        if result == "":
            return []
        return [t for t in result.split(rs) if t]
    
    def _applescript_get_events(self, selected_calendar_titles, days_ahead):
        """Fetch events from Calendar.app via AppleScript.
        
        Returns a list of event dicts in the same shape as get_calendar_events
        produces from EventKit, or None on failure.
        
        Note: AppleScript-driven event reads are slower than EventKit. This
        is the fallback path; EventKit is preferred when available.
        Recurring events are NOT auto-expanded by Calendar.app's AppleScript -
        only the master event is returned. EventKit handles expansion natively.
        """
        if not selected_calendar_titles:
            return []
        
        rs = self._AS_REC_SEP  # between events
        fs = self._AS_FLD_SEP  # between fields within an event
        
        # AppleScript list literal of calendar names. We escape backslashes and
        # double quotes so that titles containing those characters survive the
        # round-trip. AppleScript strings use \" and \\ for escaping.
        def _as_quote(s):
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
        
        cal_list_literal = "{" + ", ".join(_as_quote(t) for t in selected_calendar_titles) + "}"
        
        # The script:
        # - Builds a date window from now to now + days_ahead.
        # - For each calendar in the input list, fetches events whose start_date
        #   falls in the window.
        # - Emits one record per event with fields:
        #   calendar_title, summary, start_iso, end_iso, all_day, location, uid
        # - Dates are emitted as ISO 8601 via "(d as «class isot» as string)"
        #   which yields e.g. "2026-05-06T09:00:00".
        source = f'''
set RS to "{rs}"
set FS to "{fs}"
set targetCalendars to {cal_list_literal}
set startDate to (current date)
set endDate to startDate + ({int(days_ahead)} * days)
set output to ""
tell application "Calendar"
    repeat with calTitle in targetCalendars
        try
            tell (first calendar whose title is calTitle)
                set theEvents to (every event whose start date ≥ startDate and start date < endDate)
                repeat with e in theEvents
                    try
                        set _summary to summary of e
                    on error
                        set _summary to ""
                    end try
                    if _summary is missing value then set _summary to ""
                    try
                        set _location to location of e
                    on error
                        set _location to ""
                    end try
                    if _location is missing value then set _location to ""
                    try
                        set _uid to uid of e
                    on error
                        set _uid to ""
                    end try
                    if _uid is missing value then set _uid to ""
                    try
                        set _allday to allday event of e
                    on error
                        set _allday to false
                    end try
                    set _start to ((start date of e) as «class isot» as string)
                    set _end to ((end date of e) as «class isot» as string)
                    set _alldayStr to "false"
                    if _allday is true then set _alldayStr to "true"
                    set output to output & (calTitle as string) & FS & _summary & FS & _start & FS & _end & FS & _alldayStr & FS & _location & FS & _uid & RS
                end repeat
            end tell
        on error errMsg number errNum
            -- Skip calendars we can't read; the caller already logged the
            -- selected titles vs available ones.
        end try
    end repeat
end tell
return output
'''
        # AppleScript on busy calendars can take 10+ seconds; allow time for it.
        result = self._run_applescript(source, timeout_seconds=120)
        if result is None:
            return None
        
        events = []
        for raw in result.split(rs):
            if not raw:
                continue
            parts = raw.split(fs)
            if len(parts) < 7:
                self.log_debug(f"Skipping malformed event record: {raw!r}")
                continue
            cal_title, summary, start_iso, end_iso, all_day_str, location, uid = parts[:7]
            try:
                start_dt = self._parse_iso(start_iso)
                end_dt = self._parse_iso(end_iso)
            except Exception as e:
                self.log_debug(f"Could not parse event dates ({start_iso!r}, {end_iso!r}): {e}")
                continue
            events.append({
                'title': summary,
                'start': start_dt,
                'end': end_dt,
                'location': location,
                'all_day': (all_day_str == "true"),
                'calendar_name': cal_title,
                'event_id': uid if uid else None,
            })
        return events
    
    def _applescript_get_reminders(self, selected_list_titles):
        """Fetch incomplete reminders from Reminders.app via AppleScript.
        
        Returns a list of reminder dicts in the same shape as get_reminders
        produces from EventKit, or None on failure.
        """
        if not selected_list_titles:
            return []
        
        rs = self._AS_REC_SEP
        fs = self._AS_FLD_SEP
        
        def _as_quote(s):
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
        
        list_literal = "{" + ", ".join(_as_quote(t) for t in selected_list_titles) + "}"
        
        # Per-reminder fields: list_name, name, due_iso (or empty), priority, id
        # priority in Reminders.app: 0=none, 1/2/3=high, 4/5/6=medium, 7/8/9=low
        # We pass it through unchanged - the existing categorize logic handles it.
        source = f'''
set RS to "{rs}"
set FS to "{fs}"
set targetLists to {list_literal}
set output to ""
tell application "Reminders"
    repeat with listName in targetLists
        try
            tell (first list whose name is listName)
                set theReminders to (every reminder whose completed is false)
                repeat with r in theReminders
                    try
                        set _name to name of r
                    on error
                        set _name to ""
                    end try
                    if _name is missing value then set _name to ""
                    try
                        set _id to id of r
                    on error
                        set _id to ""
                    end try
                    if _id is missing value then set _id to ""
                    try
                        set _prio to priority of r
                    on error
                        set _prio to 0
                    end try
                    if _prio is missing value then set _prio to 0
                    set _dueStr to ""
                    try
                        set _due to due date of r
                        if _due is not missing value then
                            set _dueStr to (_due as «class isot» as string)
                        end if
                    end try
                    set output to output & (listName as string) & FS & _name & FS & _dueStr & FS & (_prio as string) & FS & _id & RS
                end repeat
            end tell
        on error errMsg number errNum
            -- Skip lists we can't read
        end try
    end repeat
end tell
return output
'''
        result = self._run_applescript(source, timeout_seconds=120)
        if result is None:
            return None
        
        reminders = []
        for raw in result.split(rs):
            if not raw:
                continue
            parts = raw.split(fs)
            if len(parts) < 5:
                self.log_debug(f"Skipping malformed reminder record: {raw!r}")
                continue
            list_name, name, due_iso, priority_str, rid = parts[:5]
            try:
                priority = int(priority_str)
            except (ValueError, TypeError):
                priority = 0
            if due_iso:
                try:
                    due_dt = self._parse_iso(due_iso)
                    due_date_str = due_dt.strftime('%A, %d %B %Y at %H:%M:%S')
                except Exception:
                    due_date_str = "undated"
            else:
                due_date_str = "undated"
            reminders.append({
                'id': rid,
                'name': name,
                'due_date_str': due_date_str,
                'list': list_name,
                'priority': priority,
            })
        return reminders
    
    @staticmethod
    def _parse_iso(s):
        """Parse an ISO-8601 datetime as produced by AppleScript's
        '«class isot» as string' coercion (e.g. '2026-05-06T09:00:00').
        Returns a naive datetime in local time, matching the existing
        nsdate_to_datetime() behaviour. Tolerates optional fractional
        seconds and trailing timezone marker as a defensive measure -
        AppleScript usually emits the bare local-time form.
        """
        from datetime import datetime
        s = s.strip()
        # Strip trailing 'Z' (UTC marker) or +HH:MM / -HH:MM if present
        if s.endswith("Z"):
            s = s[:-1]
        # Drop trailing offset like +01:00 or -05:00 if present
        if len(s) >= 6 and s[-6] in "+-" and s[-3] == ":":
            s = s[:-6]
        # Drop fractional seconds if present
        if "." in s:
            head, dot, tail = s.partition(".")
            # tail might be e.g. "000" or "123456"; strip non-digit suffix
            i = 0
            while i < len(tail) and tail[i].isdigit():
                i += 1
            s = head  # we don't preserve the microseconds; not needed for our use
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    
    # ========================================
    # Configuration Methods
    # ========================================
    
    def get_calendar_list(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Get list of available calendars. Tries EventKit first, falls back
        to AppleScript if EventKit access is denied (the typical case on
        macOS 14+ where IndigoPluginHost lacks the required Info.plist keys)."""
        
        # Use cache if less than 5 minutes old
        if self.calendar_list_cache and self.calendar_list_cache_time:
            age = (datetime.now() - self.calendar_list_cache_time).total_seconds()
            if age < 300:  # 5 minutes
                self.log_debug("Using cached calendar list")
                return self.calendar_list_cache
        
        # ---- Try EventKit first (fast path) ----
        ek_titles = self._eventkit_get_calendar_titles()
        if ek_titles is not None:
            calendar_list = sorted([(t, t) for t in ek_titles], key=lambda x: x[0])
            self.calendar_list_cache = calendar_list
            self.calendar_list_cache_time = datetime.now()
            self.logger.info(f"Fetched {len(calendar_list)} calendars from EventKit")
            return calendar_list
        
        # ---- Fall back to AppleScript ----
        self.log_debug("EventKit unavailable for calendar list, falling back to AppleScript")
        as_titles = self._applescript_get_calendar_titles()
        if as_titles is None:
            self.logger.error("Could not fetch calendar list via either EventKit or AppleScript. "
                              "Check System Settings > Privacy & Security > Automation > "
                              "IndigoPluginHost and ensure Calendar is enabled.")
            return [("Error", "Cannot access Calendar.app - check Automation permissions")]
        if not as_titles:
            return [("Error", "No calendars found")]
        
        calendar_list = sorted([(t, t) for t in as_titles], key=lambda x: x[0])
        self.calendar_list_cache = calendar_list
        self.calendar_list_cache_time = datetime.now()
        self.logger.info(f"Fetched {len(calendar_list)} calendars via AppleScript")
        return calendar_list
    
    def _eventkit_get_calendar_titles(self):
        """Try to get calendar titles via EventKit. Returns list of titles on
        success, None on any failure (so the caller can fall back to AppleScript).
        """
        try:
            from EventKit import EKEntityTypeEvent
            
            if not self._ek_calendar_access:
                if not self._ensure_access(EKEntityTypeEvent, "Calendars"):
                    return None
                self._ek_calendar_access = True
            
            store = self._get_ek_store()
            if store is None:
                return None
            
            all_calendars = store.calendarsForEntityType_(EKEntityTypeEvent)
            if not all_calendars or len(all_calendars) == 0:
                return None
            
            return [str(cal.title()) for cal in all_calendars if cal.title()]
        except Exception as e:
            self.log_debug(f"EventKit calendar list fetch failed: {e}")
            return None
            
    def get_calendar_list_for_creation(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Get writable calendars for event creation"""
        return self.get_calendar_list(filter, valuesDict, typeId, targetId)
        
    def get_device_list(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Get list of calendar devices"""
        return [(str(dev.id), dev.name) for dev in indigo.devices.iter(filter="self.calendar")]
    
    def get_calendar_devices_for_merge(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Get list of calendar and ICS calendar devices for merging"""
        devices = []
        # Add regular calendar devices
        devices.extend([(str(dev.id), dev.name) for dev in indigo.devices.iter(filter="self.calendar")])
        # Add ICS calendar devices
        devices.extend([(str(dev.id), dev.name) for dev in indigo.devices.iter(filter="self.icsCalendar")])
        return sorted(devices, key=lambda x: x[1])  # Sort by name
        
    def refresh_calendar_list(self, valuesDict, typeId, devId):
        """Callback for refresh button - clears cache"""
        self.calendar_list_cache = None
        self.calendar_list_cache_time = None
        return valuesDict
        
    # ========================================
    # Refresh Thread Management
    # ========================================
    
    def start_refresh_thread(self, device_id, interval):
        """Start background refresh thread for a device"""
        self.log_debug(f"Starting refresh thread for device {device_id} with interval {interval}s")
        
        if device_id in self.refresh_threads:
            self.log_debug(f"Stopping existing thread for device {device_id}")
            self.refresh_threads[device_id]["stop"] = True
            time.sleep(1)
            
        thread_data = {
            "stop": False,
            "interval": interval
        }
        self.refresh_threads[device_id] = thread_data
        
        thread = threading.Thread(target=self.refresh_loop, args=(device_id,))
        thread.daemon = True
        thread.start()
        self.log_debug(f"Refresh thread successfully started for device {device_id}")
        
    def refresh_loop(self, device_id):
        """Background refresh loop"""
        self.log_debug(f"Starting refresh loop for device {device_id}")
        
        while True:
            if device_id not in self.refresh_threads:
                self.log_debug(f"Device {device_id} no longer in refresh_threads, stopping loop")
                break
                
            thread_data = self.refresh_threads[device_id]
            if thread_data["stop"]:
                self.log_debug(f"Stop flag set for device {device_id}, stopping loop")
                break
                
            time.sleep(thread_data["interval"])
            
            try:
                device = indigo.devices[device_id]
                self.log_debug(f"Auto-refreshing device {device.name}")
                if device.deviceTypeId == "calendar":
                    self.refresh_calendar_events(device)
                elif device.deviceTypeId == "reminders":
                    self.refresh_reminders(device)
                elif device.deviceTypeId == "appleEvents":
                    self.refresh_apple_events(device)
            except Exception as e:
                self.logger.error(f"Error in refresh loop for device {device_id}: {e}")
                break
                
    # ========================================
    # Calendar Event Fetching
    # ========================================
    
    def format_day(self, event_date):
        """Format the day as Today, Tomorrow, or day name with ordinal"""
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + timedelta(days=1)
        event_day = event_date.replace(hour=0, minute=0, second=0, microsecond=0)
        
        if event_day == today:
            return "Today"
        elif event_day == tomorrow:
            return "Tomorrow"
        else:
            day_name = event_date.strftime("%A")
            day_number = event_date.day
            
            if 10 <= day_number % 100 <= 20:
                suffix = "th"
            else:
                suffix = {1: "st", 2: "nd", 3: "rd"}.get(day_number % 10, "th")
            
            return f"{day_name} {day_number}{suffix}"
            
    def get_calendar_events(self, device):
        """Fetch events from selected calendars. Tries EventKit first, falls
        back to AppleScript if EventKit access is denied."""
        selected_calendars = device.pluginProps.get("selectedCalendars", [])
        if not selected_calendars:
            self.logger.warning(f"No calendars selected for device {device.name}")
            return []
        
        days_ahead = int(device.pluginProps.get("daysAhead", 60))
        self.log_debug(f"Fetching events {days_ahead} days ahead for {device.name}")
        
        # ---- Try EventKit first (fast, native recurring expansion) ----
        events = self._eventkit_get_events(device, selected_calendars, days_ahead)
        source_label = "EventKit"
        
        # ---- Fall back to AppleScript ----
        if events is None:
            self.log_debug(f"{device.name}: EventKit unavailable, falling back to AppleScript")
            events = self._applescript_get_events(selected_calendars, days_ahead)
            source_label = "AppleScript"
            if events is None:
                self.logger.error(f"{device.name}: Could not fetch events via either path. "
                                  f"Check System Settings > Privacy & Security > Automation > "
                                  f"IndigoPluginHost and ensure Calendar is enabled.")
                return None
            self.log_debug(f"AppleScript returned {len(events)} events", device)
            self.log_debug("Note: Recurring events are not auto-expanded on the AppleScript "
                           "path - only the master event of each series is shown. To get "
                           "expanded recurring events, EventKit framework access is required.",
                           device)
        
        # ---- Common post-processing ----
        # Remove duplicates if enabled
        remove_duplicates = device.pluginProps.get("removeDuplicates", True)
        if remove_duplicates:
            seen = set()
            unique_events = []
            for event in events:
                key = (event['title'], event['start'], event['all_day'])
                if key not in seen:
                    seen.add(key)
                    unique_events.append(event)
            events = unique_events
        
        # Apply keyword filter
        filter_keywords = device.pluginProps.get("filterKeywords", "")
        if filter_keywords:
            original_count = len(events)
            events = self.filter_events(events, filter_keywords)
            filtered_count = original_count - len(events)
            if filtered_count > 0:
                self.log_debug(f"Filtered out {filtered_count} events based on keywords", device)
        
        # Sort by start date
        events.sort(key=lambda x: x['start'])
        
        self.log_debug(f"Returning {len(events)} events after processing (source: {source_label})", device)
        return events
    
    def _eventkit_get_events(self, device, selected_calendars, days_ahead):
        """Try to fetch events via EventKit. Returns event list on success,
        None on any failure (so the caller can fall back to AppleScript)."""
        try:
            from Foundation import NSDate, NSCalendar, NSDateComponents
            from EventKit import EKEntityTypeEvent
        except ImportError:
            self.logger.error("PyObjC EventKit not available. Please install: "
                              "pip3 install pyobjc-framework-EventKit pyobjc-framework-Cocoa")
            return None
        
        try:
            if not self._ek_calendar_access:
                if not self._ensure_access(EKEntityTypeEvent, "Calendars"):
                    return None
                self._ek_calendar_access = True
            
            store = self._get_ek_store()
            if store is None:
                return None
            
            all_calendars = store.calendarsForEntityType_(EKEntityTypeEvent)
            if all_calendars is None or len(all_calendars) == 0:
                return None
            
            # Log available calendars for debugging
            available_calendar_names = [cal.title() for cal in all_calendars]
            self.log_debug(f"Available calendars from EventKit: {available_calendar_names}", device)
            self.log_debug(f"Requested calendars from config: {selected_calendars}", device)
            
            # Filter to selected calendars
            selected_cal_objects = [cal for cal in all_calendars if cal.title() in selected_calendars]
            
            if not selected_cal_objects:
                self.logger.warning(f"No matching calendars found!")
                self.logger.warning(f"Requested: {selected_calendars}")
                self.logger.warning(f"Available: {available_calendar_names}")
                return []  # not None - we successfully queried, just no matches
            
            self.log_debug(f"Found {len(selected_cal_objects)} calendars: "
                           f"{[cal.title() for cal in selected_cal_objects]}")
            
            # Create date range
            start_date = NSDate.date()
            end_date = start_date.dateByAddingTimeInterval_(days_ahead * 24 * 60 * 60)
            
            # Create predicate to fetch events
            predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
                start_date, end_date, selected_cal_objects
            )
            
            # Fetch events (this automatically expands recurring events!)
            ek_events = store.eventsMatchingPredicate_(predicate)
            
            self.log_debug(f"EventKit returned {len(ek_events)} events (including recurring instances)")
            
            events = []
            for ek_event in ek_events:
                events.append({
                    'title': str(ek_event.title()),
                    'start': self.nsdate_to_datetime(ek_event.startDate()),
                    'end': self.nsdate_to_datetime(ek_event.endDate()),
                    'location': str(ek_event.location()) if ek_event.location() else "",
                    'all_day': ek_event.isAllDay(),
                    'calendar_name': str(ek_event.calendar().title()),
                    'event_id': str(ek_event.eventIdentifier()) if ek_event.eventIdentifier() else None
                })
            return events
        except Exception as e:
            self.log_debug(f"EventKit event fetch failed: {e}")
            return None
    
    def nsdate_to_datetime(self, nsdate):
        """Convert NSDate to Python datetime"""
        from datetime import datetime
        # NSDate's timeIntervalSince1970 gives us Unix timestamp
        timestamp = nsdate.timeIntervalSince1970()
        return datetime.fromtimestamp(timestamp)
    
    def format_cpdate(self, start_datetime, end_datetime, is_all_day):
        """Format event date/time for display
        
        Examples:
        - Today @ 09:00 - 11:00
        - Tomorrow @ 06:00 - 17:00
        - Thursday @ 14:00 - 16:00 (within next 7 days)
        - Friday 5th Dec @ 09:00 - 11:00 (8+ days away or past)
        - Today All Day
        - Saturday 14th Dec All Day
        """
        from datetime import datetime, timedelta
        
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + timedelta(days=1)
        event_day = start_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Calculate days difference
        days_diff = (event_day - today).days
        
        # Determine day format based on how far away
        if event_day == today:
            day_str = "Today"
        elif event_day == tomorrow:
            day_str = "Tomorrow"
        elif 2 <= days_diff <= 6:
            # Within next week - just day name
            day_str = start_datetime.strftime('%A')
        else:
            # 7+ days away or in the past - include date
            day_name = start_datetime.strftime('%A')
            day_num = start_datetime.strftime('%d').lstrip('0')  # Remove leading zero
            
            # Add ordinal suffix (1st, 2nd, 3rd, 4th, etc.)
            if day_num.endswith('1') and day_num != '11':
                suffix = 'st'
            elif day_num.endswith('2') and day_num != '12':
                suffix = 'nd'
            elif day_num.endswith('3') and day_num != '13':
                suffix = 'rd'
            else:
                suffix = 'th'
            
            month_abbr = start_datetime.strftime('%b')
            day_str = f"{day_name} {day_num}{suffix} {month_abbr}"
        
        # Format time part
        if is_all_day:
            return f"{day_str} All Day"
        else:
            start_time = start_datetime.strftime('%H:%M')
            end_time = end_datetime.strftime('%H:%M')
            return f"{day_str} @ {start_time} - {end_time}"
    
    def format_reminder_cpdate(self, due_date_str):
        """Format reminder due date for display
        
        Input: "Wednesday, 1 January 2025 at 14:30:00" or "undated"
        Output: 
        - "Today @ 14:30"
        - "Tomorrow @ 09:00"
        - "Thursday @ 16:00" (within next 7 days)
        - "Friday 5th Dec @ 10:30" (8+ days away or past)
        """
        from datetime import datetime, timedelta
        
        if due_date_str == "undated":
            return "No Due Date"
        
        try:
            # Parse the EventKit date string format
            # Example: "Wednesday, 1 January 2025 at 14:30:00"
            due_datetime = datetime.strptime(due_date_str, '%A, %d %B %Y at %H:%M:%S')
            
            now = datetime.now()
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            tomorrow = today + timedelta(days=1)
            week_from_now = today + timedelta(days=7)
            due_day = due_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
            
            # Calculate days difference
            days_diff = (due_day - today).days
            
            # Determine format based on how far away
            time_str = due_datetime.strftime('%H:%M')
            
            if due_day == today:
                return f"Today @ {time_str}"
            elif due_day == tomorrow:
                return f"Tomorrow @ {time_str}"
            elif 2 <= days_diff <= 6:
                # Within next week - just day name
                day_name = due_datetime.strftime('%A')
                return f"{day_name} @ {time_str}"
            else:
                # 7+ days away or in the past - include date
                day_name = due_datetime.strftime('%A')
                day_num = due_datetime.strftime('%d').lstrip('0')  # Remove leading zero
                
                # Add ordinal suffix (1st, 2nd, 3rd, 4th, etc.)
                if day_num.endswith('1') and day_num != '11':
                    suffix = 'st'
                elif day_num.endswith('2') and day_num != '12':
                    suffix = 'nd'
                elif day_num.endswith('3') and day_num != '13':
                    suffix = 'rd'
                else:
                    suffix = 'th'
                
                month_abbr = due_datetime.strftime('%b')
                return f"{day_name} {day_num}{suffix} {month_abbr} @ {time_str}"
            
        except Exception as e:
            self.logger.error(f"Error parsing reminder date '{due_date_str}': {e}")
            return due_date_str  # Return original if parsing fails

    def refresh_calendar_events(self, device):
        """Refresh calendar events and update device states"""
        self.log_debug(f"=== refresh_calendar_events called for {device.name} ===", device)
        
        debug_enabled = device.pluginProps.get("debugLogging", False)
        self.log_debug(f"Debug logging enabled: {debug_enabled}", device)
        self.log_debug(f"Refreshing calendar events for {device.name}", device)
        
        self.log_debug(f"Calling get_calendar_events...", device)
        events = self.get_calendar_events(device)
        
        # If None returned, there was an error - keep existing data
        if events is None:
            self.logger.warning(f"{device.name}: get_calendar_events returned None, keeping existing data")
            self.log_debug(f"Skipping update due to fetch error, keeping existing data", device)
            return
            
        self.log_debug(f"{device.name}: Successfully got {len(events)} events", device)
        self.log_debug(f"Got {len(events)} events from get_calendar_events", device)
        
        event_count_str = device.pluginProps.get("eventCount", "7")
        self.log_debug(f"Event count from props (raw): '{event_count_str}' (type: {type(event_count_str)})", device)
        
        try:
            event_count = int(event_count_str)
        except:
            event_count = 7
            
        self.log_debug(f"Event count setting (converted): {event_count}", device)
        
        # Limit to requested number of events
        events = events[:event_count]
        self.log_debug(f"Limited to {len(events)} events based on config", device)
        
        # Get existing state names to clear unused ones
        existing_states = list(device.states.keys())
        
        # Clear all event states first
        states_to_update = []
        for i in range(1, event_count + 1):
            states_to_update.extend([
                {"key": f"event{i:02d}Name", "value": ""},
                {"key": f"event{i:02d}CalendarName", "value": ""},
                {"key": f"event{i:02d}Location", "value": ""},
                {"key": f"event{i:02d}StartDate", "value": ""},
                {"key": f"event{i:02d}StartTime", "value": ""},
                {"key": f"event{i:02d}EndDate", "value": ""},
                {"key": f"event{i:02d}EndTime", "value": ""},
                {"key": f"event{i:02d}Duration", "value": ""},
                {"key": f"event{i:02d}AllDay", "value": "false"},
                {"key": f"event{i:02d}CPdate", "value": ""},
            ])
        
        # Update with actual event data
        for idx, event in enumerate(events):
            i = idx + 1
            
            start_date = event['start'].strftime('%Y-%m-%d')
            start_time = event['start'].strftime('%H:%M')
            end_date = event['end'].strftime('%Y-%m-%d')
            end_time = event['end'].strftime('%H:%M')
            
            # Calculate duration
            duration = event['end'] - event['start']
            hours = int(duration.total_seconds() // 3600)
            minutes = int((duration.total_seconds() % 3600) // 60)
            duration_str = f"{hours}h {minutes}m"
            
            # Format CPdate display
            cpdate_str = self.format_cpdate(event['start'], event['end'], event['all_day'])
            
            # Find and update the existing states
            for state in states_to_update:
                if state["key"] == f"event{i:02d}Name":
                    state["value"] = event['title']
                elif state["key"] == f"event{i:02d}CalendarName":
                    state["value"] = event['calendar_name']
                elif state["key"] == f"event{i:02d}Location":
                    state["value"] = event['location']
                elif state["key"] == f"event{i:02d}StartDate":
                    state["value"] = start_date
                elif state["key"] == f"event{i:02d}StartTime":
                    state["value"] = start_time
                elif state["key"] == f"event{i:02d}EndDate":
                    state["value"] = end_date
                elif state["key"] == f"event{i:02d}EndTime":
                    state["value"] = end_time
                elif state["key"] == f"event{i:02d}Duration":
                    state["value"] = duration_str
                elif state["key"] == f"event{i:02d}AllDay":
                    state["value"] = "true" if event['all_day'] else "false"
                elif state["key"] == f"event{i:02d}CPdate":
                    state["value"] = cpdate_str
        
        # Update all states at once
        device.updateStatesOnServer(states_to_update)
        
        # Update metadata states
        device.updateStateOnServer("lastUpdate", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        device.updateStateOnServer("eventCount", len(events))
        
        self.log_debug(f"=== Finished updating {len(events)} events for {device.name} ===", device)
        
        
        self.log_debug(f"=== Finished updating {len(events)} events for {device.name} ===", device)
        self.log_debug(f"Updated {len(events)} events for {device.name}", device)
    
    def refresh_merged_calendar(self, device):
        """Refresh merged calendar by combining events from multiple calendar devices"""
        self.log_debug(f"=== refresh_merged_calendar called for {device.name} ===", device)
        
        # Get source device IDs
        source_device_ids = device.pluginProps.get("sourceDevices", [])
        if isinstance(source_device_ids, str):
            source_device_ids = [source_device_ids] if source_device_ids else []
        
        if not source_device_ids:
            self.logger.warning(f"{device.name}: No source calendar devices selected")
            return
        
        # Collect events from all source devices
        all_events = []
        for dev_id_str in source_device_ids:
            try:
                dev_id = int(dev_id_str)
                source_device = indigo.devices[dev_id]
                
                # Extract events from source device states
                event_count_state = source_device.states.get("eventCount", 0)
                for i in range(1, int(event_count_state) + 1):
                    event_name = source_device.states.get(f"event{i:02d}Name")
                    if not event_name:
                        continue
                    
                    # Get all event details
                    event = {
                        'title': event_name,
                        'calendar_name': source_device.states.get(f"event{i:02d}CalendarName", ""),
                        'location': source_device.states.get(f"event{i:02d}Location", ""),
                        'start_time': source_device.states.get(f"event{i:02d}StartTime", ""),
                        'end_time': source_device.states.get(f"event{i:02d}EndTime", ""),
                        'duration': source_device.states.get(f"event{i:02d}Duration", ""),
                        'all_day': source_device.states.get(f"event{i:02d}AllDay", "false") == "true",
                        'cpdate': source_device.states.get(f"event{i:02d}CPdate", ""),
                        'source_device': source_device.name
                    }
                    all_events.append(event)
                    
            except Exception as e:
                self.logger.error(f"Error reading events from device {dev_id_str}: {e}")
        
        # Remove duplicates if enabled
        remove_duplicates = device.pluginProps.get("removeDuplicates", True)
        if remove_duplicates:
            seen = set()
            unique_events = []
            for event in all_events:
                # Use title + start time as duplicate key
                key = (event['title'], event['start_time'], event['all_day'])
                if key not in seen:
                    seen.add(key)
                    unique_events.append(event)
            all_events = unique_events
        
        # Sort by start time (parse cpdate for sorting)
        # For now, just use the order they come in
        # TODO: Could improve sorting by parsing start_time
        
        # Limit to requested event count
        max_events = int(device.pluginProps.get("eventCount", 10))
        events_to_display = all_events[:max_events]
        
        # Update device states
        states_to_update = []
        for i in range(1, max_events + 1):
            if i <= len(events_to_display):
                event = events_to_display[i - 1]
                states_to_update.append({'key': f"event{i:02d}Name", 'value': event['title']})
                states_to_update.append({'key': f"event{i:02d}CalendarName", 'value': event['calendar_name']})
                states_to_update.append({'key': f"event{i:02d}Location", 'value': event['location']})
                states_to_update.append({'key': f"event{i:02d}StartTime", 'value': event['start_time']})
                states_to_update.append({'key': f"event{i:02d}EndTime", 'value': event['end_time']})
                states_to_update.append({'key': f"event{i:02d}Duration", 'value': event['duration']})
                states_to_update.append({'key': f"event{i:02d}AllDay", 'value': "true" if event['all_day'] else "false"})
                states_to_update.append({'key': f"event{i:02d}CPdate", 'value': event['cpdate']})
            else:
                # Clear unused slots
                states_to_update.append({'key': f"event{i:02d}Name", 'value': ""})
                states_to_update.append({'key': f"event{i:02d}CalendarName", 'value': ""})
                states_to_update.append({'key': f"event{i:02d}Location", 'value': ""})
                states_to_update.append({'key': f"event{i:02d}StartTime", 'value': ""})
                states_to_update.append({'key': f"event{i:02d}EndTime", 'value': ""})
                states_to_update.append({'key': f"event{i:02d}Duration", 'value': ""})
                states_to_update.append({'key': f"event{i:02d}AllDay", 'value': "false"})
                states_to_update.append({'key': f"event{i:02d}CPdate", 'value': ""})
        
        device.updateStatesOnServer(states_to_update)
        device.updateStateOnServer("lastUpdate", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        device.updateStateOnServer("eventCount", len(events_to_display))
        
        self.log_debug(f"=== Finished updating {len(events_to_display)} merged events for {device.name} ===", device)
    
    def refresh_ics_calendar(self, device):
        """Refresh ICS calendar from external feed"""
        from datetime import datetime, timedelta
        import urllib.request
        import urllib.error
        
        self.log_debug(f"=== refresh_ics_calendar called for {device.name} ===", device)
        
        ics_url = device.pluginProps.get("icsUrl", "").strip()
        if not ics_url:
            self.logger.error(f"{device.name}: No ICS URL configured")
            return
        
        days_ahead = int(device.pluginProps.get("daysAhead", 60))
        
        try:
            # Fetch ICS feed
            self.log_debug(f"Fetching ICS feed from {ics_url}", device)
            req = urllib.request.Request(ics_url)
            req.add_header('User-Agent', 'Indigo-Calendar-Plugin/3.4.0')
            
            with urllib.request.urlopen(req, timeout=30) as response:
                ics_data = response.read().decode('utf-8')
            
            # Parse ICS data
            events = self.parse_ics_data(ics_data, days_ahead, device)
            
            if events is None:
                self.logger.warning(f"{device.name}: Failed to parse ICS data")
                return
            
            # Limit to requested count
            event_count = int(device.pluginProps.get("eventCount", 10))
            events = events[:event_count]
            
            # Update device states (same pattern as regular calendar)
            states_to_update = []
            for i in range(1, event_count + 1):
                states_to_update.extend([
                    {"key": f"event{i:02d}Name", "value": ""},
                    {"key": f"event{i:02d}CalendarName", "value": ""},
                    {"key": f"event{i:02d}Location", "value": ""},
                    {"key": f"event{i:02d}StartDate", "value": ""},
                    {"key": f"event{i:02d}StartTime", "value": ""},
                    {"key": f"event{i:02d}EndDate", "value": ""},
                    {"key": f"event{i:02d}EndTime", "value": ""},
                    {"key": f"event{i:02d}Duration", "value": ""},
                    {"key": f"event{i:02d}AllDay", "value": "false"},
                    {"key": f"event{i:02d}CPdate", "value": ""},
                ])
            
            # Update with actual event data
            for idx, event in enumerate(events):
                i = idx + 1
                
                start_date = event['start'].strftime('%Y-%m-%d')
                start_time = event['start'].strftime('%H:%M')
                end_date = event['end'].strftime('%Y-%m-%d')
                end_time = event['end'].strftime('%H:%M')
                
                # Calculate duration
                duration = event['end'] - event['start']
                hours = int(duration.total_seconds() // 3600)
                minutes = int((duration.total_seconds() % 3600) // 60)
                duration_str = f"{hours}h {minutes}m"
                
                # Format CPdate display
                cpdate_str = self.format_cpdate(event['start'], event['end'], event['all_day'])
                
                # Find and update the existing states
                for state in states_to_update:
                    if state["key"] == f"event{i:02d}Name":
                        state["value"] = event['title']
                    elif state["key"] == f"event{i:02d}CalendarName":
                        state["value"] = event['calendar_name']
                    elif state["key"] == f"event{i:02d}Location":
                        state["value"] = event['location']
                    elif state["key"] == f"event{i:02d}StartDate":
                        state["value"] = start_date
                    elif state["key"] == f"event{i:02d}StartTime":
                        state["value"] = start_time
                    elif state["key"] == f"event{i:02d}EndDate":
                        state["value"] = end_date
                    elif state["key"] == f"event{i:02d}EndTime":
                        state["value"] = end_time
                    elif state["key"] == f"event{i:02d}Duration":
                        state["value"] = duration_str
                    elif state["key"] == f"event{i:02d}AllDay":
                        state["value"] = "true" if event['all_day'] else "false"
                    elif state["key"] == f"event{i:02d}CPdate":
                        state["value"] = cpdate_str
            
            # Update all states at once
            device.updateStatesOnServer(states_to_update)
            
            # Update metadata states
            device.updateStateOnServer("lastUpdate", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            device.updateStateOnServer("eventCount", len(events))
            
            self.log_debug(f"=== Finished updating {len(events)} ICS events for {device.name} ===", device)
            
        except urllib.error.URLError as e:
            self.logger.error(f"{device.name}: Failed to fetch ICS feed: {e}")
        except Exception as e:
            self.logger.error(f"{device.name}: Error refreshing ICS calendar: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
    
    def parse_ics_data(self, ics_data, days_ahead, device):
        """Parse ICS calendar data"""
        from datetime import datetime, timedelta
        import re
        
        try:
            events = []
            now = datetime.now()
            cutoff_date = now + timedelta(days=days_ahead)
            
            # Simple ICS parser - split by VEVENT
            vevent_pattern = r'BEGIN:VEVENT(.*?)END:VEVENT'
            vevents = re.findall(vevent_pattern, ics_data, re.DOTALL)
            
            for vevent_data in vevents:
                event = {}
                
                # Extract SUMMARY (title)
                summary_match = re.search(r'SUMMARY:(.*?)(?:\r?\n(?! ))', vevent_data)
                if summary_match:
                    event['title'] = summary_match.group(1).strip()
                else:
                    continue  # Skip events without title
                
                # Extract DTSTART
                dtstart_match = re.search(r'DTSTART(?:;[^:]*)?:(.*?)(?:\r?\n)', vevent_data)
                if dtstart_match:
                    dt_str = dtstart_match.group(1).strip()
                    try:
                        if 'T' in dt_str:
                            # Has time
                            event['start'] = datetime.strptime(dt_str.replace('Z', ''), '%Y%m%dT%H%M%S')
                            event['all_day'] = False
                        else:
                            # All day
                            event['start'] = datetime.strptime(dt_str, '%Y%m%d')
                            event['all_day'] = True
                    except:
                        continue  # Skip if can't parse date
                else:
                    continue  # Skip events without start date
                
                # Extract DTEND
                dtend_match = re.search(r'DTEND(?:;[^:]*)?:(.*?)(?:\r?\n)', vevent_data)
                if dtend_match:
                    dt_str = dtend_match.group(1).strip()
                    try:
                        if 'T' in dt_str:
                            event['end'] = datetime.strptime(dt_str.replace('Z', ''), '%Y%m%dT%H%M%S')
                        else:
                            event['end'] = datetime.strptime(dt_str, '%Y%m%d')
                    except:
                        event['end'] = event['start'] + timedelta(hours=1)
                else:
                    event['end'] = event['start'] + timedelta(hours=1)
                
                # Extract LOCATION
                location_match = re.search(r'LOCATION:(.*?)(?:\r?\n(?! ))', vevent_data)
                event['location'] = location_match.group(1).strip() if location_match else ""
                
                # Set calendar name
                event['calendar_name'] = "ICS Feed"
                
                # Only include events within date range
                if event['start'] <= cutoff_date:
                    events.append(event)
            
            # Remove duplicates if enabled
            remove_duplicates = device.pluginProps.get("removeDuplicates", True)
            if remove_duplicates:
                seen = set()
                unique_events = []
                for event in events:
                    key = (event['title'], event['start'], event['all_day'])
                    if key not in seen:
                        seen.add(key)
                        unique_events.append(event)
                events = unique_events
            
            # Apply keyword filter
            filter_keywords = device.pluginProps.get("filterKeywords", "")
            if filter_keywords:
                events = self.filter_events(events, filter_keywords)
            
            # Sort by start date
            events.sort(key=lambda x: x['start'])
            
            return events
            
        except Exception as e:
            self.logger.error(f"Error parsing ICS data: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return None
        
    # ========================================
    # Device Actions
    # ========================================
    
    # ========================================
    # Apple Events (keynotes / launches -> Calendar.app)
    # ========================================
    # Discovery and planning live in apple_events.py (pure Python, unit-tested).
    # This section owns the EventKit writes and the Indigo device states.
    
    @staticmethod
    def _truthy(value):
        """Indigo config values arrive as bool or as 'true'/'false' strings."""
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes")
        return bool(value)
    
    def _apple_events_state_path(self, device):
        """JSON file remembering which dates we have already put in the calendar."""
        folder = os.path.join(indigo.server.getInstallFolderPath(),
                              "Preferences", "Plugins", self.pluginId)
        return os.path.join(folder, f"appleEvents_{device.id}.json")
    
    def refreshAppleEvents(self, pluginAction):
        """Action: check for Apple events right now."""
        try:
            device = indigo.devices[pluginAction.deviceId]
        except Exception as e:
            self.logger.error(f"Check Apple Events Now: could not find the selected device: {e}")
            return
        self.logger.info(f"{device.name}: Checking for Apple events...")
        self.refresh_apple_events(device)
    
    def refresh_apple_events(self, device):
        """One Apple Events check: read the feed, then add/upgrade calendar entries."""
        # Skip rather than queue if a check is already running (startup + timer + action)
        if not self._apple_events_lock.acquire(blocking=False):
            self.log_debug("Apple Events check already in progress - skipping", device)
            return
        try:
            self._run_apple_events_check(device)
        except Exception as e:
            self.logger.error(f"{device.name}: Apple Events check failed: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            self._apple_events_set_error(device, f"Error: {e}")
        finally:
            self._apple_events_lock.release()
    
    def _run_apple_events_check(self, device):
        from EventKit import EKEntityTypeEvent
        
        props = device.pluginProps
        cfg = apple_events.config_from_props(props)
        calendar_title = (props.get("targetCalendar") or "").strip()
        feed_url = (props.get("feedUrl") or apple_events.DEFAULT_FEED_URL).strip()
        today = datetime.now().date()
        
        if not calendar_title or calendar_title == "Error":
            self.logger.error(f"{device.name}: No target calendar selected - "
                              f"open the device settings and choose one")
            self._apple_events_set_error(device, "No calendar selected")
            return
        
        # ---- 1. Read the feed and work out what is out there ----
        self.log_debug(f"Fetching {feed_url}", device)
        try:
            items = apple_events.fetch_feed(feed_url)
        except Exception as e:
            # Don't log a warning every hour while offline - only on the first failure
            if str(device.states.get("status", "")).startswith("Feed unavailable"):
                self.log_debug(f"Feed still unavailable: {e}", device)
            else:
                self.logger.warning(f"{device.name}: Could not read {feed_url}: {e} "
                                    f"(will keep retrying)")
            self._apple_events_set_error(device, f"Feed unavailable: {e}")
            return
        
        candidates = apple_events.find_candidates(items, cfg, today)
        self.log_debug(f"{len(items)} feed items, {len(candidates)} candidate event(s): "
                       f"{[(c.kind, c.key, c.headline) for c in candidates]}", device)
        
        state_path = self._apple_events_state_path(device)
        state = apple_events.load_state(state_path)
        apple_events.prune_state(state, today)
        
        # ---- 2. Get at the calendar (only if there is something to consider) ----
        store = cal = None
        if candidates:
            if not self._ek_calendar_access:
                if self._ensure_access(EKEntityTypeEvent, "Calendars"):
                    self._ek_calendar_access = True
                else:
                    self.logger.error(f"{device.name}: No Calendars access. Open System Settings > "
                                      f"Privacy & Security > Calendars and enable IndigoPluginHost.")
                    self._apple_events_set_error(device, "No Calendars access")
                    return
            store = self._get_ek_store()
            if store is None:
                self._apple_events_set_error(device, "EventKit unavailable")
                return
            cal = self._apple_events_find_calendar(store, calendar_title)
            if cal is None:
                self.logger.error(f"{device.name}: Calendar '{calendar_title}' was not found, "
                                  f"or can't be written to")
                self._apple_events_set_error(device, f"Calendar '{calendar_title}' not found")
                return
        
        # ---- 3. Decide what to do, then do it ----
        def still_exists(key, entry):
            return self._apple_events_locate(store, cal, key, entry) is not None
        
        plan = apple_events.plan_actions(state, candidates, cfg.include_rumoured, still_exists)
        
        added = upgraded = failed = 0
        for action in plan:
            try:
                ek_id, title = self._apple_events_write(store, cal, action, cfg)
                apple_events.record_result(state, action, ek_id, title)
                # Save after every write so a later failure can't make us duplicate it
                apple_events.save_state(state_path, state)
                if action.op == "upgrade":
                    upgraded += 1
                    verb = "Upgraded"
                else:
                    added += 1
                    verb = "Added"
                self.logger.info(f"{device.name}: {verb} '{title}' on {action.key} "
                                 f"({action.candidate.kind}) in calendar '{calendar_title}'")
            except Exception as e:
                failed += 1
                self.logger.error(f"{device.name}: Could not write the {action.key} entry "
                                  f"to the calendar: {e}")
        
        if added or upgraded or failed:
            result = f"Added {added}, upgraded {upgraded}, failed {failed}"
        else:
            result = f"No changes ({len(candidates)} candidate(s) seen)"
        self.log_debug(result, device)
        self._apple_events_update_states(device, state,
                                         "OK" if not failed else "Partial failure", result)
    
    def _apple_events_find_calendar(self, store, title):
        """The first writable calendar with this title, or None."""
        from EventKit import EKEntityTypeEvent
        for cal in store.calendarsForEntityType_(EKEntityTypeEvent):
            if cal.title() == title and cal.allowsContentModifications():
                return cal
        return None
    
    def _apple_events_locate(self, store, cal, key, entry):
        """Find the EKEvent we created earlier for a saved entry, or None.
        
        Looks up by identifier first. Some calendar providers re-issue identifiers
        after syncing, so fall back to matching on day + title in the same calendar.
        """
        ek_id = entry.get("ek_id")
        if ek_id:
            event = store.eventWithIdentifier_(ek_id)
            if event is not None:
                return event
        try:
            from Foundation import NSDate
            day = datetime.strptime(key, "%Y-%m-%d")
            start = NSDate.dateWithTimeIntervalSince1970_((day - timedelta(days=1)).timestamp())
            end = NSDate.dateWithTimeIntervalSince1970_((day + timedelta(days=2)).timestamp())
            predicate = store.predicateForEventsWithStartDate_endDate_calendars_(start, end, [cal])
            for event in (store.eventsMatchingPredicate_(predicate) or []):
                if str(event.title()) == entry.get("title"):
                    return event
        except Exception as e:
            self.log_debug(f"Fallback event lookup failed: {e}")
        return None
    
    def _apple_events_write(self, store, cal, action, cfg):
        """Create (or upgrade in place) the EKEvent for one planned action.
        
        Returns (event_identifier, title).
        """
        from EventKit import EKEvent
        from Foundation import NSDate, NSURL
        
        cand = action.candidate
        title = apple_events.build_title(cand, cfg)
        
        event = None
        if action.op == "upgrade" and action.existing_entry:
            event = self._apple_events_locate(store, cal, action.key, action.existing_entry)
        if event is None:
            event = EKEvent.eventWithEventStore_(store)
            event.setCalendar_(cal)
        
        event.setTitle_(title)
        
        # Keep anything the user wrote in the notes; only replace our own block
        notes = apple_events.build_notes(cand)
        previous = str(event.notes() or "")
        if previous and "Added by the Indigo Calendars & Reminders plugin" not in previous:
            notes = notes + "\n\n" + previous
        event.setNotes_(notes)
        
        if cand.url:
            url = NSURL.URLWithString_(cand.url)
            if url is not None:
                event.setURL_(url)
        
        if cand.all_day:
            start_dt = datetime.combine(cand.event_date, datetime.min.time())   # local midnight
            end_dt = start_dt + timedelta(days=1)
            event.setAllDay_(True)
        else:
            start_dt = cand.start                      # timezone-aware (Pacific, or as stated)
            end_dt = start_dt + apple_events.EVENT_DURATION
            event.setAllDay_(False)
        event.setStartDate_(NSDate.dateWithTimeIntervalSince1970_(start_dt.timestamp()))
        event.setEndDate_(NSDate.dateWithTimeIntervalSince1970_(end_dt.timestamp()))
        
        # Informational entry: show as "free" so it doesn't block time on shared calendars.
        # (1 == EKEventAvailabilityFree; not every calendar type supports it.)
        try:
            event.setAvailability_(1)
        except Exception as e:
            self.log_debug(f"Could not set availability to free: {e}")
        
        success, error = store.saveEvent_span_commit_error_(event, 0, True, None)
        if not success:
            raise RuntimeError(f"EventKit save failed: {error}")
        return str(event.eventIdentifier()), title
    
    def _apple_events_update_states(self, device, state, status, result):
        today = datetime.now().date()
        today_key = today.isoformat()
        upcoming = [k for k in state["entries"] if k >= today_key]
        nxt = apple_events.next_event(state, today)
        days_away = -1
        if nxt:
            days_away = (datetime.strptime(nxt["date"], "%Y-%m-%d").date() - today).days
        device.updateStatesOnServer([
            {"key": "lastUpdate", "value": datetime.now().strftime('%Y-%m-%d %H:%M:%S')},
            {"key": "status", "value": status},
            {"key": "lastResult", "value": result},
            {"key": "upcomingCount", "value": len(upcoming)},
            {"key": "nextEventName", "value": nxt["title"] if nxt else ""},
            {"key": "nextEventDate", "value": nxt["date"] if nxt else ""},
            {"key": "nextEventKind", "value": nxt["kind"] if nxt else ""},
            {"key": "nextEventDaysAway", "value": days_away},
        ])
    
    def _apple_events_set_error(self, device, message):
        device.updateStatesOnServer([
            {"key": "status", "value": str(message)[:200]},
            {"key": "lastResult", "value": "No changes"},
        ])
    
    def actionControlSensor(self, action, device):
        """Handle sensor device action requests (Send Status Request)"""
        if action.sensorAction == indigo.kSensorAction.RequestStatus:
            self.log_debug(f"Status request for {device.name}", device)
            self.logger.info(f"{device.name}: Status request received")
            if device.deviceTypeId == "calendar":
                self.refresh_calendar_events(device)
            elif device.deviceTypeId == "reminders":
                self.refresh_reminders(device)
            elif device.deviceTypeId == "icsCalendar":
                self.refresh_ics_calendar(device)
            elif device.deviceTypeId == "appleEvents":
                self.refresh_apple_events(device)
    
    # ========================================
    # Event Triggers
    # ========================================
    
    def check_event_triggers_loop(self):
        """Background thread that checks for calendar event triggers every minute"""
        while not self.trigger_check_stop:
            try:
                self.check_event_triggers()
            except Exception as e:
                self.logger.error(f"Error checking event triggers: {e}")
            
            # Sleep for 60 seconds, but check stop flag every second
            for _ in range(60):
                if self.trigger_check_stop:
                    break
                time.sleep(1)
    
    def check_event_triggers(self):
        """Check all calendar devices for events that should trigger"""
        from datetime import datetime, timedelta
        
        now = datetime.now()
        
        # Check each trigger
        for trigger in indigo.triggers.iter("self.calendarEventStarting"):
            if not trigger.enabled:
                continue
            
            try:
                device_id = int(trigger.pluginProps.get("calendarDevice"))
                device = indigo.devices[device_id]
                event_slot = trigger.pluginProps.get("eventSlot", "any")
                minutes_before = int(trigger.pluginProps.get("minutesBefore", 0))
                match_name = trigger.pluginProps.get("matchEventName", "").strip().lower()
                
                # Determine which event slots to check
                if event_slot == "any":
                    slots_to_check = range(1, int(device.pluginProps.get("eventCount", 10)) + 1)
                else:
                    slots_to_check = [int(event_slot)]
                
                for slot_num in slots_to_check:
                    event_name = device.states.get(f"event{slot_num:02d}Name")
                    if not event_name:
                        continue
                    
                    # Check if event name matches (if filter specified)
                    if match_name and match_name not in event_name.lower():
                        continue
                    
                    # Get event start time
                    start_date_str = device.states.get(f"event{slot_num:02d}StartDate")
                    start_time_str = device.states.get(f"event{slot_num:02d}StartTime")
                    
                    if not start_date_str or not start_time_str:
                        continue
                    
                    try:
                        event_start = datetime.strptime(f"{start_date_str} {start_time_str}", '%Y-%m-%d %H:%M')
                    except:
                        continue
                    
                    # Calculate trigger time
                    trigger_time = event_start - timedelta(minutes=minutes_before)
                    
                    # Check if we should trigger (within the last minute)
                    time_diff = (now - trigger_time).total_seconds()
                    if 0 <= time_diff < 60:
                        # Create unique key for this event
                        trigger_key = f"{trigger.id}_{device.id}_{slot_num}_{event_start.isoformat()}"
                        
                        # Check if we've already triggered this
                        if trigger_key not in self.triggered_events:
                            # Fire the trigger
                            self.logger.info(f"Triggering event: {event_name} at {event_start}")
                            indigo.trigger.execute(trigger)
                            
                            # Mark as triggered
                            self.triggered_events[trigger_key] = now
                
                # Clean up old triggered events (older than 2 hours)
                cutoff_time = now - timedelta(hours=2)
                keys_to_remove = [k for k, v in self.triggered_events.items() if v < cutoff_time]
                for key in keys_to_remove:
                    del self.triggered_events[key]
                    
            except Exception as e:
                self.logger.error(f"Error checking trigger {trigger.name}: {e}")
    
    # ========================================
    # Actions
    # ========================================
    
    def validateCreateEventAction(self, valuesDict, typeId, devId):
        """Validate create event action before execution"""
        errors = indigo.Dict()
        
        # Event name is always required
        if not valuesDict.get("eventName", "").strip():
            errors["eventName"] = "Event name is required"
        
        # Start date is always required
        if not valuesDict.get("startDate", "").strip():
            errors["startDate"] = "Start date is required"
        
        all_day = valuesDict.get("allDay", False)
        
        # If not all-day, start time is required
        if not all_day and not valuesDict.get("startTime", "").strip():
            errors["startTime"] = "Start time is required for non-all-day events"
        
        # If not all-day and end time is specified, validate it
        if not all_day and valuesDict.get("endTime", "").strip():
            # End time specified, so we need to validate
            pass
        
        # Validate date format
        start_date = valuesDict.get("startDate", "").strip()
        if start_date:
            try:
                datetime.strptime(start_date, '%Y-%m-%d')
            except:
                errors["startDate"] = "Invalid date format. Use YYYY-MM-DD"
        
        # Validate start time format if not all-day
        if not all_day:
            start_time = valuesDict.get("startTime", "").strip()
            if start_time:
                try:
                    datetime.strptime(start_time, '%H:%M')
                except:
                    errors["startTime"] = "Invalid time format. Use HH:MM (24-hour)"
        
        # Validate end date if provided
        end_date = valuesDict.get("endDate", "").strip()
        if end_date:
            try:
                datetime.strptime(end_date, '%Y-%m-%d')
            except:
                errors["endDate"] = "Invalid date format. Use YYYY-MM-DD"
        
        # Validate end time if provided and not all-day
        if not all_day:
            end_time = valuesDict.get("endTime", "").strip()
            if end_time:
                try:
                    datetime.strptime(end_time, '%H:%M')
                except:
                    errors["endTime"] = "Invalid time format. Use HH:MM (24-hour)"
        
        if len(errors) > 0:
            return (False, valuesDict, errors)
        return (True, valuesDict)
    
    def refreshCalendar(self, pluginAction):
        """Action to manually refresh calendar"""
        device_id = int(pluginAction.props.get("targetDevice"))
        device = indigo.devices[device_id]
        self.refresh_calendar_events(device)
        
    def createEvent(self, pluginAction):
        """Action to create a new calendar event using EventKit with flexible date/time parsing"""
        from datetime import datetime, timedelta
        props = pluginAction.props
        
        target_calendar = props.get("targetCalendar")
        event_name = self.substitute_variables(props.get("eventName", "").strip())
        start_date_str = props.get("startDate", "").strip()
        start_time_str = props.get("startTime", "").strip()
        end_date_str = props.get("endDate", "").strip()
        end_time_str = props.get("endTime", "").strip()
        all_day = props.get("allDay", False)
        location = self.substitute_variables(props.get("location", "").strip())
        
        self.logger.info(f"Creating event: {event_name} on {target_calendar}")
        
        # Validate required fields
        if not event_name:
            self.logger.error("Event name is required")
            return
        if not start_date_str:
            self.logger.error("Start date is required")
            return
        
        # Parse flexible dates
        start_date = self.parse_flexible_date(start_date_str)
        if not start_date:
            self.logger.error(f"Could not parse start date: {start_date_str}")
            return
        
        # Use start date for end date if not specified
        if not end_date_str:
            end_date = start_date
        else:
            end_date = self.parse_flexible_date(end_date_str)
            if not end_date:
                self.logger.error(f"Could not parse end date: {end_date_str}")
                return
        
        # Handle time for non-all-day events
        if not all_day:
            if not start_time_str:
                self.logger.error("Start time is required for non-all-day events")
                return
            
            start_time = self.parse_flexible_time(start_time_str)
            if not start_time:
                self.logger.error(f"Could not parse start time: {start_time_str}")
                return
            
            # Calculate end time if not specified
            if not end_time_str:
                # Default to +1 hour from start
                start_dt = datetime.strptime(f"{start_date.strftime('%Y-%m-%d')} {start_time}", '%Y-%m-%d %H:%M')
                end_dt = start_dt + timedelta(hours=1)
                end_time = end_dt.strftime('%H:%M')
            else:
                end_time = self.parse_flexible_time(end_time_str)
                if not end_time:
                    self.logger.error(f"Could not parse end time: {end_time_str}")
                    return
        
        try:
            from EventKit import EKEntityTypeEvent, EKEvent
            from Foundation import NSDate
            
            # Verify access first
            if not self._ek_calendar_access:
                if self._ensure_access(EKEntityTypeEvent, "Calendars"):
                    self._ek_calendar_access = True
                else:
                    self.logger.error("Cannot create event - no Calendars access. "
                                      "Open System Settings > Privacy & Security > Calendars "
                                      "and enable IndigoPluginHost.")
                    return
            
            store = self._get_ek_store()
            if store is None:
                return
            
            # Get all calendars
            all_calendars = store.calendarsForEntityType_(EKEntityTypeEvent)
            
            # Find target calendar
            target_cal_obj = None
            for cal in all_calendars:
                if cal.title() == target_calendar:
                    target_cal_obj = cal
                    break
            
            if not target_cal_obj:
                self.logger.error(f"Could not find calendar: {target_calendar}")
                return
            
            # Create new event
            event = EKEvent.eventWithEventStore_(store)
            event.setTitle_(event_name)
            event.setCalendar_(target_cal_obj)
            
            # Set dates
            if all_day:
                # All-day event
                start_dt = start_date
                end_dt = end_date + timedelta(days=1)  # End date is exclusive for all-day
                event.setAllDay_(True)
            else:
                # Timed event
                start_dt = datetime.strptime(f"{start_date.strftime('%Y-%m-%d')} {start_time}", '%Y-%m-%d %H:%M')
                end_dt = datetime.strptime(f"{end_date.strftime('%Y-%m-%d')} {end_time}", '%Y-%m-%d %H:%M')
                event.setAllDay_(False)
            
            # Convert datetime to NSDate
            start_nsdate = NSDate.dateWithTimeIntervalSince1970_(start_dt.timestamp())
            end_nsdate = NSDate.dateWithTimeIntervalSince1970_(end_dt.timestamp())
            
            event.setStartDate_(start_nsdate)
            event.setEndDate_(end_nsdate)
            
            # Set location if provided
            if location:
                event.setLocation_(location)
            
            # Save event. PyObjC returns (success, error) for methods with
            # NSError** out-parameters.
            success, error = store.saveEvent_span_commit_error_(event, 0, True, None)
            
            if success:
                self.logger.info(f"Successfully created event: {event_name}")
            else:
                self.logger.error(f"Failed to create event: {error}")
                
        except Exception as e:
            self.logger.error(f"Error creating event: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
    
    def createReminder(self, pluginAction):
        """Action to create a new reminder using EventKit with flexible date/time parsing.

        Returns an indigo.Dict (ignored when run from the Indigo UI, available to scripts and
        other plugins calling executeAction(..., waitUntilDone=True)):
            success (bool), reminderId (str) and list (str) on success, error (str) on failure.
        """
        from datetime import datetime
        props = pluginAction.props
        
        target_list = props.get("targetList")
        reminder_name = self.substitute_variables(props.get("reminderName", "").strip())
        has_due_date = props.get("hasDueDate", True)
        due_date_str = props.get("dueDate", "").strip()
        due_time_str = props.get("dueTime", "").strip()
        priority = int(props.get("priority", 0))
        notes = self.substitute_variables(props.get("notes", "").strip())
        
        self.logger.info(f"Creating reminder: {reminder_name} on {target_list}")
        
        # Validate required fields
        if not reminder_name:
            self.logger.error("Reminder name is required")
            return self._reminder_result(False, error="Reminder name is required")
        if not target_list:
            self.logger.error("Target list is required")
            return self._reminder_result(False, error="Target list is required")
        
        try:
            from EventKit import EKEntityTypeReminder, EKReminder
            from Foundation import NSDateComponents
            
            # Verify access first
            if not self._ek_reminder_access:
                if self._ensure_access(EKEntityTypeReminder, "Reminders"):
                    self._ek_reminder_access = True
                else:
                    self.logger.error("Cannot create reminder - no Reminders access. "
                                      "Open System Settings > Privacy & Security > Reminders "
                                      "and enable IndigoPluginHost.")
                    return self._reminder_result(False, error="No Reminders access")
            
            store = self._get_ek_store()
            if store is None:
                return self._reminder_result(False, error="Could not open the EventKit store")
            
            # Get all reminder lists
            all_lists = store.calendarsForEntityType_(EKEntityTypeReminder)
            
            # Find target list
            target_list_obj = None
            for lst in all_lists:
                if lst.title() == target_list:
                    target_list_obj = lst
                    break
            
            if not target_list_obj:
                self.logger.error(f"Could not find reminder list: {target_list}")
                return self._reminder_result(False, error=f"Could not find reminder list: {target_list}")
            
            # Create new reminder
            reminder = EKReminder.reminderWithEventStore_(store)
            reminder.setTitle_(reminder_name)
            reminder.setCalendar_(target_list_obj)
            
            # Set due date if requested
            if has_due_date and due_date_str:
                # Parse flexible date
                due_date = self.parse_flexible_date(due_date_str)
                if not due_date:
                    self.logger.error(f"Could not parse due date: {due_date_str}")
                    return self._reminder_result(False, error=f"Could not parse due date: {due_date_str}")
                
                # Parse flexible time if provided
                if due_time_str:
                    due_time = self.parse_flexible_time(due_time_str)
                    if not due_time:
                        self.logger.error(f"Could not parse due time: {due_time_str}")
                        return self._reminder_result(False, error=f"Could not parse due time: {due_time_str}")
                    
                    due_datetime = datetime.strptime(f"{due_date.strftime('%Y-%m-%d')} {due_time}", '%Y-%m-%d %H:%M')
                else:
                    due_datetime = due_date
                
                # Create date components
                components = NSDateComponents.alloc().init()
                components.setYear_(due_datetime.year)
                components.setMonth_(due_datetime.month)
                components.setDay_(due_datetime.day)
                
                if due_time_str:
                    components.setHour_(due_datetime.hour)
                    components.setMinute_(due_datetime.minute)
                
                reminder.setDueDateComponents_(components)
            
            # Set priority
            if priority > 0:
                reminder.setPriority_(priority)
            
            # Set notes
            if notes:
                reminder.setNotes_(notes)
            
            # Save reminder. PyObjC returns (success, error) for methods with
            # NSError** out-parameters.
            success, error = store.saveReminder_commit_error_(reminder, True, None)
            
            if success:
                self.logger.info(f"Successfully created reminder: {reminder_name}")
                reminder_id = str(reminder.calendarItemIdentifier())
                if props.get("saveIdToVariable", False):
                    self._save_reminder_id_to_variable(props.get("idVariable", ""), reminder_id,
                                                       reminder_name)
                return self._reminder_result(True, reminderId=reminder_id,
                                             list=str(target_list))
            else:
                self.logger.error(f"Failed to create reminder: {error}")
                return self._reminder_result(False, error=f"Failed to save reminder: {error}")
                
        except Exception as e:
            self.logger.error(f"Error creating reminder: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return self._reminder_result(False, error=str(e))

    # ========================================
    # Scripting API (v4.2.11)
    # completeReminderById and getReminderLists are hidden from the Indigo UI.
    # They exist so other plugins (e.g. BatteryWatch) can call
    #     plugin.executeAction(actionId, props={...}, waitUntilDone=True)
    # and get a result back.
    # ========================================

    @staticmethod
    def _reminder_result(success, **fields):
        """Build the indigo.Dict returned to scripts/plugins that call a reminder action."""
        result = indigo.Dict()
        result["success"] = bool(success)
        for key, value in fields.items():
            result[key] = value
        return result

    def completeReminderById(self, pluginAction):
        """Hidden action: mark the reminder with the given reminderId as complete.

        props: reminderId (str) - as returned by createReminder.
        Returns an indigo.Dict: success (bool), reminderId (str), and where relevant
        alreadyComplete (bool), notFound (bool) or error (str).
        """
        reminder_id = str(pluginAction.props.get("reminderId", "")).strip()
        if not reminder_id:
            self.logger.error("completeReminderById: reminderId is required")
            return self._reminder_result(False, error="reminderId is required")
        return self._complete_reminder_by_id(reminder_id)

    def _complete_reminder_by_id(self, reminder_id):
        """Shared by completeReminderById and completeReminderFromVariable."""
        try:
            from EventKit import EKEntityTypeReminder

            if not self._ek_reminder_access:
                if self._ensure_access(EKEntityTypeReminder, "Reminders"):
                    self._ek_reminder_access = True
                else:
                    self.logger.error("Cannot complete reminder - no Reminders access.")
                    return self._reminder_result(False, error="No Reminders access")

            store = self._get_ek_store()
            if store is None:
                return self._reminder_result(False, error="Could not open the EventKit store")

            item = store.calendarItemWithIdentifier_(reminder_id)
            if item is None:
                self.logger.warning(f"No reminder with ID {reminder_id} "
                                    f"(it may have been deleted)")
                return self._reminder_result(False, reminderId=reminder_id, notFound=True,
                                             error="Reminder not found")

            name = str(item.title())
            if item.isCompleted():
                self.log_debug(f"Reminder '{name}' was already complete")
                return self._reminder_result(True, reminderId=reminder_id, alreadyComplete=True)

            item.setCompleted_(True)
            success, error = store.saveReminder_commit_error_(item, True, None)
            if success:
                self.logger.info(f"Marked reminder '{name}' as complete")
                return self._reminder_result(True, reminderId=reminder_id)

            self.logger.error(f"Failed to mark reminder '{name}' as complete: {error}")
            return self._reminder_result(False, reminderId=reminder_id,
                                         error=f"Failed to save reminder: {error}")
        except Exception as e:
            self.logger.error(f"Error completing reminder {reminder_id}: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return self._reminder_result(False, reminderId=reminder_id, error=str(e))

    # ========================================
    # Reminder ID <-> variable (v4.2.12)
    # ========================================

    def _save_reminder_id_to_variable(self, var_id, reminder_id, reminder_name):
        """Store a newly created reminder's ID in an Indigo variable."""
        if not var_id:
            self.logger.warning(f"Reminder '{reminder_name}' created, but no variable was "
                                f"selected to save its ID")
            return
        try:
            variable = indigo.variables[int(var_id)]
            indigo.variable.updateValue(variable, reminder_id)
            self.log_debug(f"Saved ID of reminder '{reminder_name}' to variable '{variable.name}'")
        except Exception as e:
            self.logger.error(f"Could not save reminder ID to variable {var_id}: {e}")

    def completeReminderFromVariable(self, pluginAction):
        """Action: complete the reminder whose ID is stored in a variable.

        An empty variable is a no-op (logged at debug level only), so this is safe to
        run from a trigger that fires repeatedly, e.g. a geofence enter.
        """
        props = pluginAction.props
        var_id = props.get("idVariable", "")
        clear_variable = props.get("clearVariable", True)

        if not var_id:
            self.logger.error("Complete Reminder from Variable: no variable selected")
            return self._reminder_result(False, error="No variable selected")

        try:
            variable = indigo.variables[int(var_id)]
        except Exception:
            self.logger.error(f"Complete Reminder from Variable: variable {var_id} not found")
            return self._reminder_result(False, error="Variable not found")

        reminder_id = str(variable.value).strip()
        if not reminder_id:
            self.log_debug(f"Variable '{variable.name}' is empty - no reminder to complete")
            return self._reminder_result(False, noReminderId=True)

        result = self._complete_reminder_by_id(reminder_id)

        # Clear on success, or if the reminder no longer exists (the stored ID is stale).
        # Leave it alone on any other failure (e.g. no Reminders access) so a retry can work.
        if clear_variable and (result.get("success") or result.get("notFound")):
            try:
                indigo.variable.updateValue(variable, "")
                self.log_debug(f"Cleared variable '{variable.name}'")
            except Exception as e:
                self.logger.error(f"Could not clear variable '{variable.name}': {e}")

        return result

    def validateActionConfigUi(self, valuesDict, typeId, actionId):
        """Validate the variable fields on the reminder actions."""
        errors = indigo.Dict()
        if typeId == "createReminder":
            if valuesDict.get("saveIdToVariable", False) and not valuesDict.get("idVariable", ""):
                errors["idVariable"] = "Select a variable to save the reminder ID to"
        elif typeId == "completeReminderFromVariable":
            if not valuesDict.get("idVariable", ""):
                errors["idVariable"] = "Select the variable holding the reminder ID"
        if errors:
            return (False, valuesDict, errors)
        return (True, valuesDict)

    def getReminderLists(self, pluginAction):
        """Hidden action: return an indigo.List of the Reminders list names."""
        titles = self._eventkit_get_reminder_list_titles()
        if titles is None:
            titles = self._applescript_get_reminder_list_titles()
        result = indigo.List()
        for title in sorted((str(t) for t in (titles or [])), key=str.lower):
            result.append(title)
        return result

    # ========================================
    # Reminders Methods
    # ========================================
    
    def get_reminder_list(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Get list of available reminder lists. Tries EventKit first, falls
        back to AppleScript if EventKit access is denied."""
        # ---- Try EventKit first (fast path) ----
        ek_titles = self._eventkit_get_reminder_list_titles()
        if ek_titles is not None:
            list_names = sorted([(t, t) for t in ek_titles], key=lambda x: x[0])
            self.logger.info(f"Fetched {len(list_names)} reminder lists from EventKit")
            return list_names
        
        # ---- Fall back to AppleScript ----
        self.log_debug("EventKit unavailable for reminder list, falling back to AppleScript")
        as_titles = self._applescript_get_reminder_list_titles()
        if as_titles is None:
            self.logger.error("Could not fetch reminder lists via either EventKit or AppleScript. "
                              "Check System Settings > Privacy & Security > Automation > "
                              "IndigoPluginHost and ensure Reminders is enabled.")
            return [("Error", "Cannot access Reminders.app - check Automation permissions")]
        if not as_titles:
            return [("Error", "No reminder lists found")]
        
        list_names = sorted([(t, t) for t in as_titles], key=lambda x: x[0])
        self.logger.info(f"Fetched {len(list_names)} reminder lists via AppleScript")
        return list_names
    
    def _eventkit_get_reminder_list_titles(self):
        """Try to get reminder list titles via EventKit. Returns list of titles
        on success, None on any failure (so the caller can fall back to AppleScript).
        """
        try:
            from EventKit import EKEntityTypeReminder
            
            if not self._ek_reminder_access:
                if not self._ensure_access(EKEntityTypeReminder, "Reminders"):
                    return None
                self._ek_reminder_access = True
            
            store = self._get_ek_store()
            if store is None:
                return None
            
            all_lists = store.calendarsForEntityType_(EKEntityTypeReminder)
            if not all_lists or len(all_lists) == 0:
                return None
            
            return [str(lst.title()) for lst in all_lists if lst.title()]
        except Exception as e:
            self.log_debug(f"EventKit reminder list fetch failed: {e}")
            return None
    
    def refresh_reminder_list(self, valuesDict, typeId, devId):
        """Force refresh of reminder list cache"""
        self.logger.info("Refreshing reminder list cache")
        return valuesDict
    
    def get_reminders(self, device):
        """Fetch reminders from Reminders.app. Tries EventKit first, falls
        back to AppleScript if EventKit access is denied."""
        selected_lists = device.pluginProps.get("selectedLists", [])
        self.log_debug(f"Selected lists config value: {selected_lists} (type: {type(selected_lists)})")
        
        # Handle if it's a string instead of a list
        if isinstance(selected_lists, str):
            selected_lists = [selected_lists] if selected_lists else []
        elif not isinstance(selected_lists, list):
            selected_lists = list(selected_lists) if selected_lists else []
        
        if not selected_lists:
            self.logger.warning(f"{device.name}: No reminder lists selected")
            return []
        
        self.log_debug(f"Processing lists: {selected_lists}")
        
        # ---- Try EventKit first (fast path) ----
        reminders = self._eventkit_get_reminders(device, selected_lists)
        source_label = "EventKit"
        
        # ---- Fall back to AppleScript ----
        if reminders is None:
            self.log_debug(f"{device.name}: EventKit unavailable for reminders, "
                           f"falling back to AppleScript")
            reminders = self._applescript_get_reminders(selected_lists)
            source_label = "AppleScript"
            if reminders is None:
                self.logger.error(f"{device.name}: Could not fetch reminders via either path. "
                                  f"Check System Settings > Privacy & Security > Automation > "
                                  f"IndigoPluginHost and ensure Reminders is enabled.")
                return None
        
        self.log_debug(f"Returning {len(reminders)} reminders (source: {source_label})", device)
        return reminders
    
    def _eventkit_get_reminders(self, device, selected_lists):
        """Try to fetch reminders via EventKit. Returns reminder list on success,
        None on any failure (so the caller can fall back to AppleScript)."""
        try:
            from Foundation import NSDate, NSPredicate, NSCalendar
            from EventKit import EKEntityTypeReminder
        except ImportError:
            self.logger.error("PyObjC EventKit not available. Please install: "
                              "pip3 install pyobjc-framework-EventKit pyobjc-framework-Cocoa")
            return None
        
        try:
            if not self._ek_reminder_access:
                if not self._ensure_access(EKEntityTypeReminder, "Reminders"):
                    return None
                self._ek_reminder_access = True
            
            store = self._get_ek_store()
            if store is None:
                return None
            
            all_lists = store.calendarsForEntityType_(EKEntityTypeReminder)
            if all_lists is None or len(all_lists) == 0:
                return None
            
            # Filter to selected lists
            selected_list_objects = [lst for lst in all_lists if lst.title() in selected_lists]
            
            if not selected_list_objects:
                self.logger.warning(f"No matching reminder lists found for {selected_lists}")
                return []  # not None - we successfully queried, just no matches
            
            self.log_debug(f"Found {len(selected_list_objects)} lists: "
                           f"{[lst.title() for lst in selected_list_objects]}")
            
            # Fetch incomplete reminders from selected lists
            predicate = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
                None, None, selected_list_objects
            )
            
            from threading import Event
            reminders_result = []
            fetch_complete = Event()
            
            def completion_handler(reminders):
                if reminders:
                    reminders_result.extend(reminders)
                fetch_complete.set()
            
            store.fetchRemindersMatchingPredicate_completion_(predicate, completion_handler)
            
            if not fetch_complete.wait(timeout=10):
                self.logger.error("Timeout fetching reminders from EventKit")
                return None
            
            self.log_debug(f"EventKit returned {len(reminders_result)} incomplete reminders", device)
            
            reminders = []
            for ek_reminder in reminders_result:
                due_date = ek_reminder.dueDateComponents()
                if due_date:
                    due_date_obj = NSCalendar.currentCalendar().dateFromComponents_(due_date)
                    due_date_str = self.nsdate_to_datetime(due_date_obj).strftime('%A, %d %B %Y at %H:%M:%S')
                else:
                    due_date_str = "undated"
                
                priority = ek_reminder.priority()
                reminders.append({
                    'id': str(ek_reminder.calendarItemIdentifier()),
                    'name': str(ek_reminder.title()),
                    'due_date_str': due_date_str,
                    'list': str(ek_reminder.calendar().title()),
                    'priority': priority,
                })
            return reminders
        except Exception as e:
            self.log_debug(f"EventKit reminder fetch failed: {e}")
            return None
    
    def categorize_and_sort_reminders(self, reminders, device):
        """Categorize reminders by date and optionally prioritize high priority items within each day"""
        from datetime import datetime, timedelta
        
        # Get priority config for date-based categories
        priority_today     = int(device.pluginProps.get("priorityToday", 1))
        priority_tomorrow  = int(device.pluginProps.get("priorityTomorrow", 2))
        priority_future    = int(device.pluginProps.get("priorityFuture", 3))
        priority_yesterday = int(device.pluginProps.get("priorityYesterday", 4))
        priority_older     = int(device.pluginProps.get("priorityOlder", 5))
        priority_undated   = int(device.pluginProps.get("priorityUndated", 6))
        
        # Check if high priority items should be shown first within each day
        prioritize_high = device.pluginProps.get("prioritizeHighPriority", False)
        
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + timedelta(days=1)
        yesterday = today - timedelta(days=1)
        
        # Categorize reminders
        categorized = []
        
        for reminder in reminders:
            app_priority = reminder.get('priority', 0)
            is_high_priority = (app_priority == 1)  # 1 = high priority in Reminders.app
            
            # Categorize by date only (ignore app priority for categorization)
            if reminder['due_date_str'] == 'undated':
                category = 'undated'
                priority = priority_undated
            else:
                # Parse date and categorize
                try:
                    due_datetime = datetime.strptime(reminder['due_date_str'], '%A, %d %B %Y at %H:%M:%S')
                    due_day = due_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
                    
                    if due_day == today:
                        category = 'today'
                        priority = priority_today
                    elif due_day == tomorrow:
                        category = 'tomorrow'
                        priority = priority_tomorrow
                    elif due_day > tomorrow:
                        category = 'future'
                        priority = priority_future
                    elif due_day == yesterday:
                        category = 'yesterday'
                        priority = priority_yesterday
                    else:  # before yesterday
                        category = 'older'
                        priority = priority_older
                except:
                    # If parsing fails, treat as undated
                    category = 'undated'
                    priority = priority_undated
            
            # Get sort date for secondary sorting
            if reminder['due_date_str'] == 'undated':
                sort_date = datetime.max
            else:
                try:
                    sort_date = datetime.strptime(reminder['due_date_str'], '%A, %d %B %Y at %H:%M:%S')
                except:
                    sort_date = datetime.max
            
            categorized.append({
                **reminder,
                '_category': category,
                '_priority': priority,
                '_sort_date': sort_date,
                '_is_high_priority': is_high_priority
            })
        
        # Sort by:
        # 1. Priority category (1=highest)
        # 2. If enabled, high priority flag (False=0 comes after True=1, so we invert)
        # 3. Due date (earliest first)
        if prioritize_high:
            categorized.sort(key=lambda x: (x['_priority'], not x['_is_high_priority'], x['_sort_date']))
        else:
            categorized.sort(key=lambda x: (x['_priority'], x['_sort_date']))
        
        return categorized


    def refresh_reminders(self, device):
        """Refresh reminders and update device states"""
        self.log_debug(f"=== refresh_reminders called for {device.name} ===", device)
        
        reminders = self.get_reminders(device)
        
        if reminders is None:
            self.logger.warning(f"{device.name}: Failed to fetch reminders, keeping existing data")
            return
        
        # Categorize and sort
        sorted_reminders = self.categorize_and_sort_reminders(reminders, device)
        
        # Get count from config
        reminder_count = int(device.pluginProps.get("reminderCount", 10))
        
        # Clear all reminder states first
        for i in range(1, reminder_count + 1):
            device.updateStateOnServer(f"reminder{i:02d}Name", "")
            device.updateStateOnServer(f"reminder{i:02d}Date", "")
            device.updateStateOnServer(f"reminder{i:02d}CPdate", "")
            device.updateStateOnServer(f"reminder{i:02d}Id", "")
        
        # Update with new data
        for i, reminder in enumerate(sorted_reminders[:reminder_count], 1):
            # Parse the due date string to create CPdate format
            cpdate_str = self.format_reminder_cpdate(reminder['due_date_str'])
            
            device.updateStateOnServer(f"reminder{i:02d}Name", reminder['name'])
            device.updateStateOnServer(f"reminder{i:02d}Date", reminder['due_date_str'])
            device.updateStateOnServer(f"reminder{i:02d}CPdate", cpdate_str)
            device.updateStateOnServer(f"reminder{i:02d}Id", reminder['id'])
        
        # Update metadata
        device.updateStateOnServer("lastUpdate", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        device.updateStateOnServer("reminderCount", len(sorted_reminders))
        
        self.log_debug(f"=== Finished updating {len(sorted_reminders)} reminders for {device.name} ===", device)
    
    def markReminderComplete(self, pluginAction):
        """Mark a reminder as complete by ID using EventKit"""
        # Get device and reminder slot from action
        device_id = int(pluginAction.props.get("reminderDevice", 0))
        reminder_slot = pluginAction.props.get("reminderSlot", "01")
        
        if not device_id:
            self.logger.error("Reminder device is required")
            return
        
        # Get the device
        device = indigo.devices.get(device_id)
        if not device:
            self.logger.error(f"Device {device_id} not found")
            return
        
        # Get the reminder ID from the device state
        state_key = f"reminder{reminder_slot}Id"
        reminder_id = device.states.get(state_key, "").strip()
        
        if not reminder_id:
            self.logger.error(f"No reminder ID found in {device.name} {state_key}")
            return
        
        # Get the reminder name for logging
        name_key = f"reminder{reminder_slot}Name"
        reminder_name = device.states.get(name_key, "Unknown")
        
        try:
            from EventKit import EKEntityTypeReminder
            
            # Verify access first
            if not self._ek_reminder_access:
                if self._ensure_access(EKEntityTypeReminder, "Reminders"):
                    self._ek_reminder_access = True
                else:
                    self.logger.error("Cannot mark reminder complete - no Reminders access.")
                    return
            
            store = self._get_ek_store()
            if store is None:
                return
            
            # Get all reminders to find the one with matching ID
            all_lists = store.calendarsForEntityType_(EKEntityTypeReminder)
            
            # Fetch all reminders (we need to find by our stored ID)
            predicate = store.predicateForRemindersInCalendars_(all_lists)
            
            from threading import Event
            reminders_result = []
            fetch_complete = Event()
            
            def completion_handler(reminders):
                if reminders:
                    reminders_result.extend(reminders)
                fetch_complete.set()
            
            store.fetchRemindersMatchingPredicate_completion_(predicate, completion_handler)
            
            # Wait for fetch
            if not fetch_complete.wait(timeout=10):
                self.logger.error("Timeout fetching reminders from EventKit")
                return
            
            # Find the reminder with matching ID
            target_reminder = None
            for reminder in reminders_result:
                if str(reminder.calendarItemIdentifier()) == reminder_id:
                    target_reminder = reminder
                    break
            
            if not target_reminder:
                self.logger.error(f"Reminder with ID {reminder_id} not found")
                return
            
            # Mark as complete
            target_reminder.setCompleted_(True)
            
            # Save changes. PyObjC returns (success, error) for methods with
            # NSError** out-parameters.
            success, error = store.saveReminder_commit_error_(target_reminder, True, None)
            
            if success:
                self.logger.info(f"Marked reminder '{reminder_name}' as complete")
                # Refresh the device to show updated list
                self.refresh_reminders(device)
            else:
                self.logger.error(f"Failed to mark reminder as complete: {error}")
                
        except Exception as e:
            self.logger.error(f"Error marking reminder complete: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
    
    def snoozeReminder(self, pluginAction):
        """Snooze a reminder by changing its due date using EventKit"""
        # Get device and reminder slot from action
        device_id = int(pluginAction.props.get("reminderDevice", 0))
        reminder_slot = pluginAction.props.get("reminderSlot", "01")
        days = int(pluginAction.props.get("snoozeDays", 0))
        hours = int(pluginAction.props.get("snoozeHours", 0))
        
        if not device_id:
            self.logger.error("Reminder device is required")
            return
        
        # Get the device
        device = indigo.devices.get(device_id)
        if not device:
            self.logger.error(f"Device {device_id} not found")
            return
        
        # Get the reminder ID from the device state
        state_key = f"reminder{reminder_slot}Id"
        reminder_id = device.states.get(state_key, "").strip()
        
        if not reminder_id:
            self.logger.error(f"No reminder ID found in {device.name} {state_key}")
            return
        
        # Get the reminder name for logging
        name_key = f"reminder{reminder_slot}Name"
        reminder_name = device.states.get(name_key, "Unknown")
        
        try:
            from EventKit import EKEntityTypeReminder
            from Foundation import NSCalendar, NSDateComponents
            from datetime import datetime, timedelta
            
            # Verify access first
            if not self._ek_reminder_access:
                if self._ensure_access(EKEntityTypeReminder, "Reminders"):
                    self._ek_reminder_access = True
                else:
                    self.logger.error("Cannot snooze reminder - no Reminders access.")
                    return
            
            store = self._get_ek_store()
            if store is None:
                return
            
            # Get all reminders to find the one with matching ID
            all_lists = store.calendarsForEntityType_(EKEntityTypeReminder)
            predicate = store.predicateForRemindersInCalendars_(all_lists)
            
            from threading import Event
            reminders_result = []
            fetch_complete = Event()
            
            def completion_handler(reminders):
                if reminders:
                    reminders_result.extend(reminders)
                fetch_complete.set()
            
            store.fetchRemindersMatchingPredicate_completion_(predicate, completion_handler)
            
            # Wait for fetch
            if not fetch_complete.wait(timeout=10):
                self.logger.error("Timeout fetching reminders from EventKit")
                return
            
            # Find the reminder with matching ID
            target_reminder = None
            for reminder in reminders_result:
                if str(reminder.calendarItemIdentifier()) == reminder_id:
                    target_reminder = reminder
                    break
            
            if not target_reminder:
                self.logger.error(f"Reminder with ID {reminder_id} not found")
                return
            
            # Get current due date or use now
            due_date_components = target_reminder.dueDateComponents()
            if due_date_components:
                due_date = NSCalendar.currentCalendar().dateFromComponents_(due_date_components)
                new_due = self.nsdate_to_datetime(due_date) + timedelta(days=days, hours=hours)
            else:
                new_due = datetime.now() + timedelta(days=days, hours=hours)
            
            # Create new date components
            new_components = NSDateComponents.alloc().init()
            new_components.setYear_(new_due.year)
            new_components.setMonth_(new_due.month)
            new_components.setDay_(new_due.day)
            new_components.setHour_(new_due.hour)
            new_components.setMinute_(new_due.minute)
            
            # Set new due date
            target_reminder.setDueDateComponents_(new_components)
            
            # Save changes. PyObjC returns (success, error) for methods with
            # NSError** out-parameters.
            success, error = store.saveReminder_commit_error_(target_reminder, True, None)
            
            if success:
                self.logger.info(f"Snoozed reminder '{reminder_name}' by {days} days and {hours} hours")
                # Refresh the device to show updated list
                self.refresh_reminders(device)
            else:
                self.logger.error(f"Failed to snooze reminder: {error}")
                
        except Exception as e:
            self.logger.error(f"Error snoozing reminder: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
    
    def setDateOnUndated(self, pluginAction):
        """Set due date/time on all undated reminders in selected lists"""
        import re
        from datetime import datetime, timedelta
        
        # Get selected lists
        selected_lists = pluginAction.props.get("selectedLists", [])
        if isinstance(selected_lists, str):
            selected_lists = [selected_lists] if selected_lists else []
        
        if not selected_lists:
            self.logger.error("No reminder lists selected")
            return
        
        # Determine the target date/time based on source
        date_source = pluginAction.props.get("dateSource", "specific")
        
        try:
            if date_source == "specific":
                # Parse specific date and time
                date_str = pluginAction.props.get("specificDate", "").strip()
                time_str = pluginAction.props.get("specificTime", "10:30").strip()
                
                if not date_str:
                    self.logger.error("Specific date is required")
                    return
                
                target_datetime = datetime.strptime(f"{date_str} {time_str}", '%Y-%m-%d %H:%M')
                
            elif date_source == "daysFromNow":
                # Calculate date X days from now
                days = int(pluginAction.props.get("daysFromNow", 1))
                time_str = pluginAction.props.get("daysFromNowTime", "10:30").strip()
                
                # Parse time
                time_parts = time_str.split(':')
                hour = int(time_parts[0])
                minute = int(time_parts[1])
                
                # Calculate target date
                target_datetime = datetime.now() + timedelta(days=days)
                target_datetime = target_datetime.replace(hour=hour, minute=minute, second=0, microsecond=0)
                
            elif date_source == "variable":
                # Parse variable format %%v:12345%%
                var_str = pluginAction.props.get("variableValue", "").strip()
                
                # Extract variable ID
                match = re.match(r'%%v:(\d+)%%', var_str)
                if not match:
                    self.logger.error(f"Invalid variable format: {var_str}. Expected: %%v:12345%%")
                    return
                
                var_id = int(match.group(1))
                
                # Get variable value
                try:
                    variable = indigo.variables[var_id]
                    var_value = variable.value.strip()
                except:
                    self.logger.error(f"Variable {var_id} not found")
                    return
                
                # Parse variable value (expected format: YYYY-MM-DD HH:MM)
                try:
                    target_datetime = datetime.strptime(var_value, '%Y-%m-%d %H:%M')
                except:
                    self.logger.error(f"Invalid date format in variable: {var_value}. Expected: YYYY-MM-DD HH:MM")
                    return
            else:
                self.logger.error(f"Invalid date source: {date_source}")
                return
            
            self.logger.info(f"Target date/time: {target_datetime.strftime('%Y-%m-%d %H:%M')}")
            
        except Exception as e:
            self.logger.error(f"Error parsing date/time: {e}")
            return
        
        # Now fetch undated reminders from selected lists and set their dates
        try:
            from EventKit import EKEntityTypeReminder
            from Foundation import NSDateComponents
            
            # Verify access first
            if not self._ek_reminder_access:
                if self._ensure_access(EKEntityTypeReminder, "Reminders"):
                    self._ek_reminder_access = True
                else:
                    self.logger.error("Cannot update reminders - no Reminders access.")
                    return
            
            store = self._get_ek_store()
            if store is None:
                return
            
            # Get all reminder lists
            all_lists = store.calendarsForEntityType_(EKEntityTypeReminder)
            
            # Filter to selected lists
            selected_list_objects = [lst for lst in all_lists if lst.title() in selected_lists]
            
            if not selected_list_objects:
                self.logger.warning(f"No matching reminder lists found for {selected_lists}")
                return
            
            self.logger.info(f"Processing lists: {[lst.title() for lst in selected_list_objects]}")
            
            # Fetch all reminders from selected lists
            predicate = store.predicateForRemindersInCalendars_(selected_list_objects)
            
            from threading import Event
            reminders_result = []
            fetch_complete = Event()
            
            def completion_handler(reminders):
                if reminders:
                    reminders_result.extend(reminders)
                fetch_complete.set()
            
            store.fetchRemindersMatchingPredicate_completion_(predicate, completion_handler)
            
            # Wait for fetch
            if not fetch_complete.wait(timeout=10):
                self.logger.error("Timeout fetching reminders from EventKit")
                return
            
            # Filter for incomplete, undated reminders
            undated_count = 0
            for reminder in reminders_result:
                # EKReminder uses the standard Objective-C BOOL getter convention:
                # the property is named 'completed' but the accessor method is
                # 'isCompleted'. PyObjC exposes the accessor, not a synthesised
                # 'completed()' wrapper. The predicate already filters to
                # incomplete reminders so this is a belt-and-braces check.
                if reminder.isCompleted():
                    continue  # Skip completed reminders
                
                due_date = reminder.dueDateComponents()
                if due_date is not None:
                    continue  # Skip reminders that already have a date
                
                # This reminder is undated - set the date
                new_components = NSDateComponents.alloc().init()
                new_components.setYear_(target_datetime.year)
                new_components.setMonth_(target_datetime.month)
                new_components.setDay_(target_datetime.day)
                new_components.setHour_(target_datetime.hour)
                new_components.setMinute_(target_datetime.minute)
                
                reminder.setDueDateComponents_(new_components)
                
                # Save the reminder. PyObjC translates Objective-C NSError**
                # out-parameters into an extra tuple element on the return,
                # so saveReminder:commit:error: returns (success, error)
                # rather than taking error as an input.
                success, error = store.saveReminder_commit_error_(reminder, True, None)
                
                if success:
                    undated_count += 1
                    self.log_debug(f"Set date on reminder: {reminder.title()}")
                else:
                    self.logger.error(f"Failed to save reminder '{reminder.title()}': {error}")
            
            self.logger.info(f"Set dates on {undated_count} undated reminders to {target_datetime.strftime('%Y-%m-%d %H:%M')}")
            
        except Exception as e:
            self.logger.error(f"Error setting dates on undated reminders: {e}")
            import traceback
            self.logger.error(traceback.format_exc())

    def test_reminders_access(self):
        """Test whether we have *any* path to Reminders data, EventKit or
        AppleScript. Used by deviceStartComm to decide whether to attempt the
        initial refresh on a reminders device.
        """
        try:
            from EventKit import EKEntityTypeReminder
            
            # EventKit fast path
            if self._ek_reminder_access:
                self.log_debug("Reminders access available via EventKit")
                return True
            if self._ensure_access(EKEntityTypeReminder, "Reminders"):
                self._ek_reminder_access = True
                self.logger.info("Reminders access detected via EventKit (granted after startup)")
                return True
            
            # AppleScript fallback
            count = self._applescript_count_reminder_lists()
            if count is not None:
                self.log_debug(f"Reminders access available via AppleScript ({count} lists visible)")
                return True
            
            self.logger.error("Cannot access Reminders via either EventKit or AppleScript.")
            self.logger.error("Check System Settings > Privacy & Security > Automation > "
                              "IndigoPluginHost and ensure Reminders is enabled.")
            return False
                
        except Exception as e:
            self.logger.error(f"Error testing Reminders access: {e}")
            self.logger.error("Check System Settings > Privacy & Security > Automation > IndigoPluginHost")
            return False

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        """Validate device configuration before saving"""
        errors = indigo.Dict()
        
        if typeId == "reminders":
            # Collect all priorities (now 6 date-based categories)
            priorities = [
                int(valuesDict.get("priorityToday", 1)),
                int(valuesDict.get("priorityTomorrow", 2)),
                int(valuesDict.get("priorityFuture", 3)),
                int(valuesDict.get("priorityYesterday", 4)),
                int(valuesDict.get("priorityOlder", 5)),
                int(valuesDict.get("priorityUndated", 6))
            ]
            
            # Check for duplicates
            if len(priorities) != len(set(priorities)):
                error_msg = "Each category must have a unique priority (1-6)"
                errors["priorityToday"] = error_msg
                errors["priorityTomorrow"] = error_msg
                errors["priorityFuture"] = error_msg
                errors["priorityYesterday"] = error_msg
                errors["priorityOlder"] = error_msg
                errors["priorityUndated"] = error_msg
        
        if typeId == "appleEvents":
            target = (valuesDict.get("targetCalendar") or "").strip()
            if not target or target == "Error":
                errors["targetCalendar"] = "Choose the calendar to add Apple events to"
            
            if not (valuesDict.get("eventName") or "").strip():
                errors["eventName"] = "Enter an event name, e.g. Apple Event"
            
            prefix = (valuesDict.get("rumouredPrefix") or "").strip()
            suffix = (valuesDict.get("rumouredSuffix") or "").strip()
            valuesDict["rumouredPrefix"] = prefix
            valuesDict["rumouredSuffix"] = suffix
            if self._truthy(valuesDict.get("includeRumoured", False)) and not (prefix or suffix):
                message = ("Add a label before and/or after the name so rumoured events "
                           "can't be mistaken for confirmed ones")
                errors["rumouredPrefix"] = message
                errors["rumouredSuffix"] = message
            
            feed_url = (valuesDict.get("feedUrl") or "").strip()
            if not feed_url.lower().startswith(("http://", "https://")):
                errors["feedUrl"] = "Enter a full URL starting with http:// or https://"
        
        if len(errors) > 0:
            return (False, valuesDict, errors)
        return (True, valuesDict)
