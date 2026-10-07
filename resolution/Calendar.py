"""!
@file Calendar.py
@brief The calendar (see CONTEXT.md): the block clock turned into a date. Pure functions over the
    loaded rules (rules.toml's [time] and [[calendar_month]]) and the clock's current_block --
    read-only; advancing time is DM_Time.py's job. See docs/downtime.md's "The block clock".

    Everything derives from current_block alone: day number, block-in-day, hour and day/night. A
    setting authoring no [[calendar_month]] table (ex: Rules/Zombie/) gets the bare "day N" and
    None for year/month/day_of_month -- the "still works unauthored" fallback every optional
    rules.toml table follows.
"""

DEFAULT_TIME_RULES = {"hours_per_day": 24, "daylight_hours": 16, "blocks_per_day": 3, "starting_year": 1}


def time_rules(rules):
    """!
    @brief rules.toml's own [time] table, defaulted the way every other optional table is when a
        setting doesn't author one -- 24 hours/day, 16 of them daylight, 3 blocks/day (an 8-hour
        block), and a "starting_year" of 1.
    @return {hours_per_day, daylight_hours, blocks_per_day, starting_year}.
    """
    return rules.get("time", DEFAULT_TIME_RULES)


def date_from_day(rules, day_number):
    """!
    @brief Converts an absolute day count into a calendar date against [[calendar_month]] (an
        ordered list of {name, days} entries -- nothing assumes a 12-month/365-day shape). Day 0 is
        the 1st of the first authored month in [time]'s "starting_year"; the count wraps into a new
        year once every authored month's days (summed) are used up.
    @return {"year", "month", "day_of_month"} (1-indexed), or None if the setting authors no
        [[calendar_month]] table (or one summing to 0 days, which can't be divided into).
    """
    months = rules.get("calendar_month", [])
    days_per_year = sum(month.get("days", 0) for month in months)
    if not months or days_per_year <= 0:
        return None
    starting_year = time_rules(rules).get("starting_year", 1)
    year = day_number // days_per_year + starting_year
    day_in_year = day_number % days_per_year
    for month in months:
        month_days = month.get("days", 0)
        if day_in_year < month_days:
            return {"year": year, "month": month.get("name", ""), "day_of_month": day_in_year + 1}
        day_in_year -= month_days
    return None  # unreachable -- day_in_year < days_per_year always lands in some month


def day_of_year(rules, month_name, day_of_month):
    """!
    @brief The inverse of date_from_day's month-walk -- how many days into the year month_name's
        day_of_month falls. Used only to seed the clock from a scenario's own start_month/start_day.
    @param month_name A [[calendar_month]] "name" (case-sensitive, exactly as authored).
    @param day_of_month 1-indexed day within that month.
    @return A 0-indexed day-of-year offset, or None if there is no [[calendar_month]] table, no
        such month, or day_of_month falls outside that month's "days".
    """
    offset = 0
    for month in rules.get("calendar_month", []):
        month_days = month.get("days", 0)
        if month.get("name") == month_name:
            if not (1 <= day_of_month <= month_days):
                return None
            return offset + (day_of_month - 1)
        offset += month_days
    return None


def time_state(rules, current_block):
    """!
    @brief The moment on the block clock for current_block. Day/night is read off actual elapsed
        hours against [time]'s daylight_hours, not a fixed block-index parity: a block counts as
        daytime if it *starts* before daylight_hours, and one straddling dusk reads as whichever
        it began in. "date_label" is a ready-made human-readable string either way, so narration
        never re-derives the calendar-vs-bare-day branch itself.
    @return {day, block_in_day, hour, is_day, blocks_per_day, hours_per_day, year, month,
            day_of_month, date_label}.
    """
    rules_time = time_rules(rules)
    blocks_per_day = max(1, rules_time.get("blocks_per_day", 3))
    hours_per_day = rules_time.get("hours_per_day", 24)
    hours_per_block = hours_per_day / blocks_per_day
    block_in_day = current_block % blocks_per_day
    hour = block_in_day * hours_per_block
    day = current_block // blocks_per_day
    date = date_from_day(rules, day)
    if date:
        date_label = f"day {date['day_of_month']} of {date['month']}, Year {date['year']}"
    else:
        date_label = f"day {day}"
    return {
        "day": day,
        "block_in_day": block_in_day,
        "hour": hour,
        "is_day": hour < rules_time.get("daylight_hours", 16),
        "blocks_per_day": blocks_per_day,
        "hours_per_day": hours_per_day,
        "year": date["year"] if date else None,
        "month": date["month"] if date else None,
        "day_of_month": date["day_of_month"] if date else None,
        "date_label": date_label,
    }
