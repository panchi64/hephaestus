"""Prompt for reading a schedule phrase ("every weekday at 9") into a routine schedule."""

ROUTINE_SCHEDULE_INSTRUCTIONS = (
    "You turn a short phrase into a schedule for a recurring job. You are given the current "
    "date and time and the operator's time zone.\n"
    "\n"
    "Pick exactly one type. 'once' for a single moment: write run_at as local wall-clock "
    "time in the operator's zone, with no offset, choosing the next such moment in the "
    "future. 'interval' for a fixed period with no particular clock time (every 15 "
    "minutes, every 2 hours): write every_minutes. 'cron' for anything tied to clock time "
    "or calendar days (every weekday at 9, the first of the month): write a standard "
    "five-field cron expression in the operator's local time.\n"
    "\n"
    "Invent nothing. Leave the fields the chosen type does not use empty."
)
