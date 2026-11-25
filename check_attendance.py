import calendar
import csv
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import sys

MSG_LINE_RE = re.compile(
    r"^(?P<year>\d{4})\.\s*(?P<month>\d{1,2})\.\s*(?P<day>\d{1,2})\.\s*"
    r"(?P<ampm>오전|오후)\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}),\s*"
    r"(?P<sender>[^:]+?)\s*:\s*(?P<content>.*)$"
)

FINE_PER_ABSENT_DAY = 1000
FINE_THRESHOLD_DAYS = 3

DEFAULT_HOLIDAYS = [
    "2026-01-01",
    "2026-02-16",
    "2026-02-17",
    "2026-02-18",
    "2026-03-01",
    "2026-05-05",
    "2026-05-24",
    "2026-08-15",
    "2026-09-24",
    "2026-09-25",
    "2026-09-26",
    "2026-10-03",
    "2026-10-09",
    "2026-12-25",
]

DEFAULT_OFFLINE_ATTENDANCE: dict[str, list[str]] = {}


def get_app_dir() -> Path:
    """스크립트 실행 시엔 파일 위치, exe 패키징 시엔 exe 위치 반환"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def parse_to_24h(ampm: str, hour: int, minute: int) -> tuple[int, int]:
    if ampm == "오전":
        h = 0 if hour == 12 else hour
    else:
        h = 12 if hour == 12 else hour + 12
    return h, minute


def parse_messages(txt_path: Path) -> list[tuple[datetime, str, str]]:
    """(작성시각_datetime, 발신자, 전체메시지내용) 리스트 반환"""
    messages: list[tuple[datetime, str, str]] = []
    cur_dt: datetime | None = None
    cur_sender: str | None = None
    cur_lines: list[str] = []

    def flush() -> None:
        if cur_dt is not None and cur_sender is not None:
            content = "\n".join(cur_lines).strip()
            if content:
                messages.append((cur_dt, cur_sender, content))

    with txt_path.open("r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.rstrip("\r\n")
            m = MSG_LINE_RE.match(line)
            if m:
                flush()
                y = int(m.group("year"))
                mo = int(m.group("month"))
                d = int(m.group("day"))
                h, mi = parse_to_24h(
                    m.group("ampm"), int(m.group("hour")), int(m.group("minute"))
                )
                cur_dt = datetime(y, mo, d, h, mi)
                cur_sender = m.group("sender").strip()
                cur_lines = [m.group("content")]
            else:
                if cur_dt is not None:
                    cur_lines.append(line)

    flush()
    return messages


def parse_year_month(year_month_str: str) -> tuple[int, int]:
    parts = year_month_str.strip().split("-")
    if len(parts) != 2:
        raise ValueError("월 형식은 YYYY-MM 이어야 합니다. (예: 2026-11)")
    year, month = int(parts[0]), int(parts[1])
    if not (1 <= month <= 12):
        raise ValueError("월(MM)은 01~12 사이여야 합니다.")
    return year, month


def get_month_dates(year_month_str: str) -> list[str]:
    """'YYYY-MM' -> 해당 월의 달력 날짜('YYYY-MM-DD') 리스트"""
    year, month = parse_year_month(year_month_str)
    last_day = calendar.monthrange(year, month)[1]
    return [f"{year:04d}-{month:02d}-{day:02d}" for day in range(1, last_day + 1)]


def get_weeks_for_month(year_month_str: str) -> list[tuple[str, list[date]]]:
    """
    'YYYY-MM'에 귀속되는 주간(일~토) 목록 반환.
    규칙: 주의 종료일(토요일)이 속한 월(YYYY-MM)을 해당 주의 귀속 월로 봄.
    달이 넘어가는 주(예: 11/29(일)~12/05(토))는 종료일 기준 다음 달(2026-12)로 이월.
    """
    year, month = parse_year_month(year_month_str)
    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])

    days_since_sunday = (first_day.weekday() + 1) % 7
    candidate_sunday = first_day - timedelta(days=days_since_sunday)

    weeks: list[tuple[str, list[date]]] = []
    while True:
        week_days = [candidate_sunday + timedelta(days=i) for i in range(7)]
        week_start = week_days[0]
        week_end = week_days[-1]

        if (week_end.year, week_end.month) > (year, month):
            break

        if (week_end.year, week_end.month) == (year, month):
            label = f"{len(weeks) + 1}주({week_start.strftime('%m-%d')}~{week_end.strftime('%m-%d')})"
            weeks.append((label, week_days))

        candidate_sunday += timedelta(days=7)

        if candidate_sunday > last_day + timedelta(days=7):
            break

    return weeks


def get_attendance_window(target_date: date) -> tuple[datetime, datetime, str]:
    """target_date -> (당일 06:00, 익일 06:00, 'MMDD')"""
    start_dt = datetime(target_date.year, target_date.month, target_date.day, 6, 0, 0)
    end_dt = start_dt + timedelta(days=1)
    mmdd = f"{target_date.month:02d}{target_date.day:02d}"
    return start_dt, end_dt, mmdd


def calculate_fine(absent_days: int) -> int:
    """주간 미출석일이 3일 이상이면 미출석일 * 1,000원"""
    if absent_days >= FINE_THRESHOLD_DAYS:
        return absent_days * FINE_PER_ABSENT_DAY
    return 0


def load_offline_attendance(offline_path: Path) -> dict[str, set[str]]:
    """{'YYYY-MM-DD': {'이름1', '이름2'}} 형태로 로드"""
    raw = json.loads(offline_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("offline_attendance.json은 {'YYYY-MM-DD': ['이름']} 형식이어야 합니다.")

    parsed: dict[str, set[str]] = {}
    for date_str, attendees in raw.items():
        if not isinstance(attendees, list):
            raise ValueError(f"'{date_str}' 항목의 값은 이름 리스트여야 합니다.")
        parsed[date_str.strip()] = {str(name).strip() for name in attendees if str(name).strip()}
    return parsed


def evaluate_dates_attendance(
    messages: list[tuple[datetime, str, str]],
    members: list[str],
    target_dates: list[date],
    holidays: set[str],
    offline_map: dict[str, set[str]],
) -> dict[date, dict[str, bool]]:
    """주어진 날짜 목록에 대한 멤버별 출석 여부 반환"""
    if not target_dates:
        return {}

    sorted_dates = sorted(set(target_dates))
    range_start = datetime(
        sorted_dates[0].year, sorted_dates[0].month, sorted_dates[0].day, 6, 0, 0
    )
    range_end = (
        datetime(
            sorted_dates[-1].year, sorted_dates[-1].month, sorted_dates[-1].day, 6, 0, 0
        )
        + timedelta(days=1)
    )

    scoped_messages = [
        (dt, content) for dt, _, content in messages if range_start <= dt < range_end
    ]

    result: dict[date, dict[str, bool]] = {}
    for d in sorted_dates:
        d_str = d.isoformat()
        if d_str in holidays:
            result[d] = {name: True for name in members}
            continue

        offline_attendees = offline_map.get(d_str, set())
        start_dt, end_dt, mmdd = get_attendance_window(d)
        day_contents = [
            content for dt, content in scoped_messages if start_dt <= dt < end_dt
        ]

        result[d] = {
            name: (
                (name in offline_attendees)
                or any((mmdd in content) and (name in content) for content in day_contents)
            )
            for name in members
        }

    return result


def build_monthly_report(
    messages: list[tuple[datetime, str, str]],
    members: list[str],
    year_month_str: str,
    holidays: set[str],
    offline_map: dict[str, set[str]],
) -> tuple[
    list[str],
    list[tuple[str, list[date]]],
    dict[str, dict[str, bool]],
    dict[str, dict[str, tuple[int, int]]],
]:
    month_date_strs = get_month_dates(year_month_str)
    weeks = get_weeks_for_month(year_month_str)

    needed_dates: set[date] = {date.fromisoformat(s) for s in month_date_strs}
    for _, w_days in weeks:
        needed_dates.update(w_days)

    evaluated = evaluate_dates_attendance(
        messages, members, sorted(needed_dates), holidays, offline_map
    )

    month_matrix: dict[str, dict[str, bool]] = {name: {} for name in members}
    for d_str in month_date_strs:
        d_obj = date.fromisoformat(d_str)
        for name in members:
            month_matrix[name][d_str] = evaluated[d_obj][name]

    weekly_fines: dict[str, dict[str, tuple[int, int]]] = {name: {} for name in members}
    for name in members:
        for w_label, w_days in weeks:
            w_absent = sum(1 for d_obj in w_days if not evaluated[d_obj][name])
            w_fine = calculate_fine(w_absent)
            weekly_fines[name][w_label] = (w_absent, w_fine)

    return month_date_strs, weeks, month_matrix, weekly_fines


def format_monthly_tables(
    year_month_str: str,
    month_date_strs: list[str],
    weeks: list[tuple[str, list[date]]],
    members: list[str],
    month_matrix: dict[str, dict[str, bool]],
    weekly_fines: dict[str, dict[str, tuple[int, int]]],
    holidays: set[str],
    offline_map: dict[str, set[str]],
) -> tuple[str, dict[str, int]]:
    total_days = len(month_date_strs)
    lines: list[str] = []
    total_fines: dict[str, int] = {}

    perfect_members: list[str] = []
    for name in members:
        day_map = month_matrix[name]
        present_days = sum(1 for ok in day_map.values() if ok)
        if total_days > 0 and present_days == total_days:
            perfect_members.append(name)

    month_holidays = sorted(d for d in month_date_strs if d in holidays)
    month_offline_dates = sorted(
        d for d in month_date_strs if offline_map.get(d)
    )
    offline_summary = (
        ", ".join(
            f"{d[5:]}({', '.join(sorted(offline_map[d]))})"
            for d in month_offline_dates
        )
        if month_offline_dates
        else "없음"
    )

    lines.append(
        f"=== [{year_month_str}] 월간 출석 및 벌금 현황 (기준: 매일 06:00 ~ 익일 06:00, 주간: 일~토 / 종료일 기준 월 이월) ==="
    )
    lines.append(
        f"공휴일 인정({len(month_holidays)}일): {', '.join(month_holidays) if month_holidays else '없음'}"
    )
    lines.append(f"오프라인 모임 참석 인정: {offline_summary}")
    lines.append(
        f"벌금 규칙: 주간(일~토) 미출석 {FINE_THRESHOLD_DAYS}일 이상 시 미출석일당 {FINE_PER_ABSENT_DAY:,}원"
    )
    lines.append(
        f"출석 100% 올출자 ({len(perfect_members)}명): {', '.join(perfect_members) if perfect_members else '없음'}"
    )
    lines.append("")

    # 1. 월간 요약 및 총 벌금
    name_width = max([len(m) for m in members] + [4])
    summary_header = (
        f"{'이름':<{name_width}s} | {'출석':>5s} | {'미출석':>4s} | {'출석률':>6s} | {'월총벌금':>9s} | 월내미출석일"
    )
    lines.append("[1. 월간 요약 및 총 벌금]")
    lines.append(summary_header)
    lines.append("-" * len(summary_header))

    for name in members:
        day_map = month_matrix[name]
        present_days = sum(1 for ok in day_map.values() if ok)
        absent_list = [d for d in month_date_strs if not day_map[d]]
        absent_days = len(absent_list)
        rate = (present_days / total_days * 100) if total_days else 0.0

        month_fine = sum(fine for _, fine in weekly_fines[name].values())
        total_fines[name] = month_fine

        missing_dd = ", ".join(d.split("-")[2] for d in absent_list) or "없음"
        lines.append(
            f"{name:<{name_width}s} | {present_days:>2d}/{total_days:<2d} | {absent_days:>3d}일 | {rate:>5.1f}% | {month_fine:>8,d}원 | {missing_dd}"
        )

    # 2. 주간(일~토) 단위 벌금 내역
    lines.append("")
    lines.append("[2. 주간(일~토) 단위 미출석 및 벌금 내역 - 토요일 귀속 월 기준]")
    if weeks:
        week_cols = [label for label, _ in weeks]
        week_header = f"{'이름':<{name_width}s} | " + " | ".join(
            f"{col:^17s}" for col in week_cols
        )
        lines.append(week_header)
        lines.append("-" * len(week_header))

        for name in members:
            cells: list[str] = []
            for label, _ in weeks:
                w_absent, w_fine = weekly_fines[name][label]
                cell = f"결{w_absent}일/{w_fine:,}원"
                cells.append(f"{cell:^17s}")
            lines.append(f"{name:<{name_width}s} | " + " | ".join(cells))
    else:
        lines.append("해당 월에 귀속되는 주간(일~토)이 없습니다.")

    # 3. 월간 일자별 O/H/X 현황표
    lines.append("")
    lines.append("[3. 월간 일자별 출석 현황표 (O: 출석, H: 공휴일 인정, X: 미출석)]")
    day_cols = [d.split("-")[2] for d in month_date_strs]
    grid_header = f"{'이름':<{name_width}s} | " + " ".join(day_cols)
    lines.append(grid_header)
    lines.append("-" * len(grid_header))

    for name in members:
        marks: list[str] = []
        for d in month_date_strs:
            if d in holidays:
                marks.append(" H")
            elif month_matrix[name][d]:
                marks.append(" O")
            else:
                marks.append(" X")
        lines.append(f"{name:<{name_width}s} | " + " ".join(marks))

    return "\n".join(lines), total_fines


def save_monthly_files(
    reports_dir: Path,
    year_month_str: str,
    month_date_strs: list[str],
    weeks: list[tuple[str, list[date]]],
    members: list[str],
    month_matrix: dict[str, dict[str, bool]],
    weekly_fines: dict[str, dict[str, tuple[int, int]]],
    holidays: set[str],
    total_fines: dict[str, int],
    report_text: str,
) -> tuple[Path, Path]:
    reports_dir.mkdir(exist_ok=True)
    txt_out = reports_dir / f"출석현황_{year_month_str}.txt"
    csv_out = reports_dir / f"출석현황_{year_month_str}.csv"

    txt_out.write_text(report_text + "\n", encoding="utf-8")

    total_days = len(month_date_strs)
    week_labels = [label for label, _ in weeks]
    week_fine_cols = [f"{label}_벌금" for label in week_labels]

    with csv_out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "이름",
                "출석일수",
                "월내미출석일수",
                "전체일수",
                "출석률(%)",
                "월총벌금(원)",
                *week_fine_cols,
                *month_date_strs,
            ]
        )
        for name in members:
            present_days = sum(1 for d in month_date_strs if month_matrix[name][d])
            absent_days = total_days - present_days
            rate = round((present_days / total_days * 100), 1) if total_days else 0.0
            marks = [
                "H" if d in holidays else ("O" if month_matrix[name][d] else "X")
                for d in month_date_strs
            ]
            w_fines_row = [weekly_fines[name][label][1] for label in week_labels]
            writer.writerow(
                [
                    name,
                    present_days,
                    absent_days,
                    total_days,
                    rate,
                    total_fines[name],
                    *w_fines_row,
                    *marks,
                ]
            )

    return txt_out, csv_out


def main() -> None:
    app_dir = get_app_dir()
    exports_dir = app_dir / "exports"
    reports_dir = app_dir / "reports"
    exports_dir.mkdir(exist_ok=True)

    members_path = app_dir / "members.json"
    if not members_path.exists():
        members_path.write_text('["홍길동", "성춘향"]\n', encoding="utf-8")
        print(f"members.json이 없어 기본 파일을 생성했습니다: {members_path}")

    holidays_path = app_dir / "holidays.json"
    if not holidays_path.exists():
        holidays_path.write_text(
            json.dumps(DEFAULT_HOLIDAYS, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"holidays.json이 없어 기본 파일을 생성했습니다: {holidays_path}")

    offline_path = app_dir / "offline_attendance.json"
    if not offline_path.exists():
        offline_path.write_text(
            json.dumps(DEFAULT_OFFLINE_ATTENDANCE, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"offline_attendance.json이 없어 기본 파일을 생성했습니다: {offline_path}")

    members = json.loads(members_path.read_text(encoding="utf-8"))
    if not members:
        print("members.json에 등록된 인원이 없습니다.")
        return

    holidays = set(json.loads(holidays_path.read_text(encoding="utf-8")))
    offline_map = load_offline_attendance(offline_path)

    txt_files = sorted(exports_dir.glob("*.txt"))
    if not txt_files:
        print(f"'{exports_dir}' 폴더에 카카오톡 대화 내보내기(.txt) 파일을 넣어주세요.")
        return

    latest_txt = txt_files[-1]
    print(f"대상 파일: {latest_txt.name}")

    now = datetime.now()
    default_attendance_date = (
        (now.date() - timedelta(days=1)) if now.hour < 6 else now.date()
    )
    default_ym = default_attendance_date.strftime("%Y-%m")

    user_input = input(
        f"조회할 월(YYYY-MM)을 입력하세요 (기본값 {default_ym}, Enter 시 적용): "
    ).strip()
    year_month = user_input or default_ym

    messages = parse_messages(latest_txt)
    month_date_strs, weeks, month_matrix, weekly_fines = build_monthly_report(
        messages, members, year_month, holidays, offline_map
    )
    report_text, total_fines = format_monthly_tables(
        year_month,
        month_date_strs,
        weeks,
        members,
        month_matrix,
        weekly_fines,
        holidays,
        offline_map,
    )

    print("\n" + report_text)

    txt_out, csv_out = save_monthly_files(
        reports_dir,
        year_month,
        month_date_strs,
        weeks,
        members,
        month_matrix,
        weekly_fines,
        holidays,
        total_fines,
        report_text,
    )
    print(f"\n저장 완료:\n- 텍스트표: {txt_out}\n- CSV파일: {csv_out}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\n오류가 발생했습니다: {exc}")
    finally:
        if getattr(sys, "frozen", False):
            input("\n종료하려면 Enter 키를 누르세요...")