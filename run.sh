#!/data/data/com.termux/files/usr/bin/bash
# Scheduled wrapper for Termux. Runs the tracker once per day (after 09:00).
# If pages fail it retries on the next scheduled slot; the 4th attempt reports.
cd "$(dirname "$0")" || exit 1
termux-wake-lock 2>/dev/null
source ~/.tracker_env
STATE=~/.tracker_state
today=$(date +%F)
[ "$(date +%H)" -lt 9 ] && exit 0
d=""; n=0; done=""
[ -f "$STATE" ] && read -r d n done < "$STATE"
[ "$d" = "$today" ] && [ "$done" = "done" ] && exit 0
[ "$d" != "$today" ] && n=0
n=$((n+1))
if [ "$n" -ge 4 ]; then export TRACKER_FINAL=1; else export TRACKER_FINAL=0; fi
python tracker.py
rc=$?
if [ $rc -eq 0 ]; then echo "$today $n done" > "$STATE"; else echo "$today $n" > "$STATE"; fi
termux-wake-unlock 2>/dev/null
