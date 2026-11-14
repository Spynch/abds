"""Generate deterministic CSV datasets using only the Python standard library."""
import calendar
import csv
import time
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path

TABLES = ('dim_realty', 'dim_gauges', 'dim_tariffs', 'dim_users',
          'fact_gauge_bindings', 'fact_meter_readings', 'fact_invoice')
START = date(2023, 11, 1)
END = date(2026, 10, 31)
REPLACEMENT = date(2025, 11, 1)
CREATED = '2023-11-01 00:00:00'


def generate(directory=Path('data')):
    """Write cumulative readings, corrections, monthly usage and tariff history."""
    started = time.monotonic()
    directory.mkdir(parents=True, exist_ok=True)
    counts = dict.fromkeys(TABLES, 0)
    with ExitStack() as stack:
        writers = {table: csv.writer(stack.enter_context(
            (directory / (table + '.csv')).open('w', encoding='utf-8', newline='', buffering=1024*1024)
        ), lineterminator='\n') for table in TABLES}

        def emit(table, row):
            """Write one CSV row and track the physical dataset size."""
            writers[table].writerow(row)
            counts[table] += 1

        for identifier in range(1, 1101):
            emit('dim_gauges', [identifier, 'Mercury 230', 'Russia', CREATED])
        emit('dim_tariffs', [1, 'Day-Night', 550, 275, CREATED,
                            '2025-11-01 00:00:00', CREATED, CREATED])
        emit('dim_tariffs', [1, 'Day-Night', 620, 310, '2025-11-01 00:00:00',
                            '2099-01-01 00:00:00', CREATED, '2025-11-01 00:00:00'])
        dates = [START + timedelta(days=offset) for offset in range((END-START).days+1)]
        for identifier in range(1, 1001):
            nonresidential = identifier % 10 == 0
            emit('dim_realty', [identifier, f'Moscow, Energy street, {1+(identifier-1)//50}',
                               40 + identifier % 80, 'nrcp' if nonresidential else 'flat',
                               '' if nonresidential else str(1+(identifier-1)%50),
                               0 if nonresidential else 1+identifier%4,
                               1+identifier%16, 1+identifier%4, CREATED])
            emit('dim_users', [identifier, f'Owner {identifier}', f'+7900{identifier:07d}',
                              '', CREATED, CREATED])
            emit('fact_gauge_bindings', [identifier, identifier, CREATED, 1, 1])
            if identifier <= 100:
                emit('fact_gauge_bindings', [identifier, identifier, CREATED, -1, 1])
                emit('fact_gauge_bindings', [1000+identifier, identifier,
                                           '2025-11-01 00:00:00', 1, 2])
            cumulative_day = cumulative_night = monthly_day = monthly_night = 0
            for offset, current in enumerate(dates):
                gauge = 1000+identifier if identifier <= 100 and current >= REPLACEMENT else identifier
                if identifier <= 100 and current == REPLACEMENT:
                    cumulative_day = cumulative_night = 0
                usage_day = 4 + (identifier + offset) % 7
                usage_night = 1 + (identifier + offset) % 4
                cumulative_day += usage_day
                cumulative_night += usage_night
                monthly_day += usage_day
                monthly_night += usage_night
                iso = current.isoformat()
                corrected = (identifier * 17 + offset * 31) % 100 < 3
                prefix = [identifier, identifier, 1, gauge, iso]
                emit('fact_meter_readings', prefix + [cumulative_day + int(corrected),
                                                     cumulative_night, iso + ' 12:00:00'])
                if corrected:
                    emit('fact_meter_readings', prefix + [cumulative_day, cumulative_night,
                                                         iso + ' 12:00:01'])
                if current.day == calendar.monthrange(current.year, current.month)[1]:
                    next_month = current + timedelta(days=1)
                    deadline = next_month.replace(day=10).isoformat()
                    price_day, price_night = (550, 275) if current < REPLACEMENT else (620, 310)
                    emit('fact_invoice', [identifier, identifier, 1, gauge, monthly_day,
                                          monthly_night, monthly_day*price_day+monthly_night*price_night,
                                          deadline, iso + ' 23:00:00'])
                    monthly_day = monthly_night = 0
    for table, count in counts.items():
        print(f'{table}: {count:,}')
    print(f'Generated in {time.monotonic()-started:.2f}s')
    return counts


if __name__ == '__main__':
    generate()
