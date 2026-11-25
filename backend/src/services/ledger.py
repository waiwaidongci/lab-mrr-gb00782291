"""Reconcile subscription changes reported by multiple sites into one ledger.

Each site uploads its own subscription-change ledger. A handover order records
that, from an effective date, one site hands its subscriptions to another. The
merged ledger is a pure function of the *set* of uploaded files, so the same
files always produce the same ledger regardless of arrival order. Records that
are no longer owned by the uploading site are withdrawn (越界) or withheld (暂扣)
in favour of the site that actually owns the subscription on that date.
"""

import csv
import hashlib
import io
import json
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from src.models.database import add_event, engine
from src.models.tables import ledger_files, ledger_snapshots, ledger_state
from src.services.data import csv_records, save_revision_in
from src.services.domain import MAX_ROWS, SubscriptionState, _validate_transition, parse_state

HANDOVER_FIELDS = ('from_site', 'to_site', 'effective_date', 'subscription_id')
MERGE_VERSION = 1
MAX_SITE_NAME = 80
# Serialises ingests inside one process; PostgreSQL row locks cover multiple
# instances. Every ingest commits its file and the complete snapshot together.
INGEST_LOCK = threading.Lock()


@dataclass(frozen=True)
class LedgerRecord:
    site: str
    file_name: str
    file_checksum: str
    row: int
    state: SubscriptionState


@dataclass(frozen=True)
class Handover:
    file_name: str
    file_checksum: str
    row: int
    from_site: str
    to_site: str
    effective_date: date
    subscription_id: str


def _clean_site(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('请填写场地名称。')
    site = value.strip()
    if site == '*':
        raise ValueError('场地名称不能为 *，星号仅用于移交单的订阅范围。')
    if len(site) > MAX_SITE_NAME:
        raise ValueError(f'场地名称最多 {MAX_SITE_NAME} 个字符。')
    return site


def _checksum(kind, site, records):
    payload = json.dumps({'kind': kind, 'site': site, 'records': records}, sort_keys=True,
                         ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def site_checksum(site, records):
    return _checksum('site', site, records)


def handover_records(text):
    """Validate a handover CSV and return normalised rows."""
    reader = csv.DictReader(io.StringIO(text.lstrip('﻿'), newline=''), strict=True)
    try:
        fieldnames = reader.fieldnames
    except csv.Error as error:
        raise ValueError('移交单 CSV 表头的引号格式有误。') from error
    if not fieldnames or len(fieldnames) != len(HANDOVER_FIELDS) or set(fieldnames) != set(HANDOVER_FIELDS):
        raise ValueError('移交单表头须为 ' + '、'.join(HANDOVER_FIELDS) + '。')
    rows = []
    try:
        for row in reader:
            if None in row:
                raise ValueError(f'移交单第 {reader.line_num} 行列数多于表头。')
            number = reader.line_num
            from_site = _clean_site(row['from_site'])
            to_site = _clean_site(row['to_site'])
            if from_site == to_site:
                raise ValueError(f'移交单第 {number} 行的移交方与接手方相同，请核对。')
            effective = row['effective_date']
            if not isinstance(effective, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', effective):
                raise ValueError(f'移交单第 {number} 行生效日无效，请按 YYYY-MM-DD 填写。')
            try:
                date.fromisoformat(effective)
            except ValueError as error:
                raise ValueError(f'移交单第 {number} 行生效日无效，请按 YYYY-MM-DD 填写实际日期。') from error
            subscription = row['subscription_id']
            if not isinstance(subscription, str) or not subscription.strip():
                raise ValueError(f'移交单第 {number} 行缺少订阅编号，请填写订阅编号或 * 表示整处场地。')
            rows.append({'from_site': from_site, 'to_site': to_site,
                         'effective_date': effective, 'subscription_id': subscription.strip()})
            if len(rows) > 5000:
                raise ValueError('单份移交单最多 5000 行。')
    except csv.Error as error:
        raise ValueError('移交单 CSV 引号或字段格式有误。') from error
    if not rows:
        raise ValueError('移交单需要至少一条数据。')
    return rows


def handover_checksum(records):
    return _checksum('handover', None, records)


def _state_row(state):
    return {
        'account_id': state.account_id,
        'subscription_id': state.subscription_id,
        'tier': state.tier,
        'effective_date': state.effective_date.isoformat(),
        'status': 'active' if state.active else 'inactive',
        'mrr_minor': state.mrr,
    }


def _entry(record_type, source, file_name, file_checksum, subscription, result, note,
           state=None, row=0):
    return {
        '记录类型': record_type,
        '来源场地': source,
        '来源文件': file_name,
        '文件校验': file_checksum[:12],
        '订阅编号': subscription,
        '账户': state.account_id if state else '',
        '档位': state.tier if state else '',
        '生效日': state.effective_date.isoformat() if state else '',
        '状态': ('active' if state.active else 'inactive') if state else '',
        '合同月额（分）': state.mrr if state else '',
        '采纳结果': result,
        '说明': note,
        '行号': row,
    }


def _reading(site_files, handover_files):
    records = []
    for site_file in site_files:
        for index, row in enumerate(site_file['records'], 1):
            label = (f"文件《{site_file['name']}》第 {index} 条记录的"
                     f"订阅 {row.get('subscription_id')}")
            records.append(LedgerRecord(site_file['site'], site_file['name'], site_file['checksum'],
                                        index, parse_state(label, row)))
    handovers = []
    for handover_file in handover_files:
        for index, row in enumerate(handover_file['records'], 1):
            handovers.append(Handover(handover_file['name'], handover_file['checksum'], index,
                                      row['from_site'], row['to_site'],
                                      date.fromisoformat(row['effective_date']),
                                      row['subscription_id']))
    return records, handovers


def merge(site_files, handover_files):
    """Return the merged ledger from the full set of site ledger and handover files.

    ``site_files`` items are ``{site, name, checksum, records}`` where records use
    the six subscription columns. ``handover_files`` items are
    ``{name, checksum, records}`` with the four handover columns.
    """
    records, handovers = _reading(site_files, handover_files)
    by_subscription = defaultdict(list)
    for record in records:
        by_subscription[record.state.subscription_id].append(record)

    labels = {}
    issues = []
    adopted_by_subscription = {}
    applied = set()

    for subscription in sorted(by_subscription):
        group = by_subscription[subscription]
        earliest = min(group, key=lambda item: (item.state.effective_date, item.site,
                                                item.file_checksum, item.row))
        origin = earliest.site
        tied = sorted({item.site for item in group
                       if item.state.effective_date == earliest.state.effective_date})
        if len(tied) > 1:
            issues.append({'级别': '警告', '订阅编号': subscription,
                           '说明': f"{earliest.state.effective_date} 同日出现{'、'.join(tied)}的记录，"
                                   f"已按 {origin} 作为原场地结转。"})

        applicable = [item for item in handovers
                      if item.subscription_id in ('*', subscription)]
        applicable.sort(key=lambda item: (item.effective_date, item.from_site, item.to_site,
                                          item.subscription_id, item.file_checksum, item.row))
        owner = origin
        transitions = []
        for handover in applicable:
            if handover.from_site != owner:
                if handover.subscription_id == subscription:
                    issues.append({'级别': '警告', '订阅编号': subscription,
                                   '说明': f"移交单《{handover.file_name}》第 {handover.row} 行"
                                           f"{handover.from_site}→{handover.to_site}"
                                           f"（{handover.effective_date}）的移交方与当前归属 {owner} 不符，已忽略。"})
                continue
            transitions.append(handover)
            applied.add((handover.file_checksum, handover.row))
            owner = handover.to_site
        participants = {origin}
        for handover in transitions:
            participants.update((handover.from_site, handover.to_site))

        def owner_at(when):
            holder = origin
            for handover in transitions:
                if handover.effective_date <= when:
                    holder = handover.to_site
                else:
                    break
            return holder

        def relinquished_by(site, when):
            """True when site handed the subscription off on or before when."""
            return any(item.from_site == site and item.effective_date <= when
                       for item in transitions)

        def takes_over_after(site, when):
            """True when site takes the subscription over on a date after when."""
            return any(item.to_site == site and item.effective_date > when
                       for item in transitions)

        adopted = []
        for record in group:
            key = (record.file_checksum, record.row)
            when = record.state.effective_date
            if record.site == owner_at(when):
                labels[key] = ('已采纳', '归属场地在该生效日有效。')
                adopted.append(record)
                continue
            if relinquished_by(record.site, when):
                labels[key] = ('撤下越界', f"该场地已移交，{when} 的记录不再归属本场地。")
            elif transitions and record.site not in participants:
                labels[key] = ('非归属场地', f"订阅 {subscription} 从未归属 {record.site}。")
            elif takes_over_after(record.site, when):
                labels[key] = ('暂扣待移交', f"记录早于 {record.site} 接手日期，移交生效前不采纳。")
            elif transitions:
                labels[key] = ('暂扣待移交', f"该场地未在 {when} 持有订阅 {subscription}。")
            else:
                labels[key] = ('暂扣待移交', '移交单未到，暂按原场地记录，待移交生效后补回。')

        same_day = defaultdict(list)
        for record in adopted:
            same_day[record.state.effective_date].append(record)
        surviving = []
        for when in sorted(same_day):
            day = sorted(same_day[when], key=lambda item: (item.file_checksum, item.row))
            surviving.append(day[0])
            for extra in day[1:]:
                labels[(extra.file_checksum, extra.row)] = ('重复忽略', f"{when} 已有采纳记录，本条重复。")
                issues.append({'级别': '错误', '订阅编号': subscription,
                               '说明': f"订阅 {subscription} 在 {when} 有多条同场地采纳记录，请合并为当天最终状态。"})

        previous = None
        for record in surviving:
            try:
                _validate_transition(previous, record.state)
            except ValueError as error:
                issues.append({'级别': '错误', '订阅编号': subscription, '说明': str(error)})
            previous = record.state

        adopted_by_subscription[subscription] = surviving

    merged = [record for subscription in sorted(adopted_by_subscription)
              for record in sorted(adopted_by_subscription[subscription],
                                   key=lambda item: (item.state.effective_date, item.file_checksum, item.row))]

    if len(merged) > MAX_ROWS:
        issues.append({'级别': '错误', '订阅编号': '',
                       '说明': f'合并后的生效记录 {len(merged)} 条，超过 {MAX_ROWS} 条上限，请拆分数据。'})

    entries = []
    for record in records:
        result, note = labels[(record.file_checksum, record.row)]
        entries.append(_entry('生效记录', record.site, record.file_name, record.file_checksum,
                              record.state.subscription_id, result, note, record.state, record.row))
    for handover in handovers:
        applied_here = (handover.file_checksum, handover.row) in applied
        entries.append(_entry('移交单', f'{handover.from_site}→{handover.to_site}',
                              handover.file_name, handover.file_checksum,
                              handover.subscription_id,
                              '已生效' if applied_here else '未生效',
                              (f"自 {handover.effective_date} 起由 {handover.to_site} 接手。"
                               if applied_here else
                               f"{handover.from_site} 未持有该范围订阅，或移交方与当前归属不符。"),
                              row=handover.row))
        if not applied_here:
            issues.append({'级别': '警告', '订阅编号': handover.subscription_id if handover.subscription_id != '*' else '',
                           '说明': f"移交单《{handover.file_name}》第 {handover.row} 行未生效："
                                   f"{handover.from_site} 未持有该范围订阅，或对应场地台账尚未导入。"})

    entries.sort(key=lambda item: (item['记录类型'] != '移交单', item['订阅编号'],
                                   item['生效日'], item['文件校验'], item['行号']))
    merged_records = [_state_row(record.state) for record in merged]

    counts = defaultdict(int)
    for entry in entries:
        counts[entry['采纳结果']] += 1
    summary = {
        'sites': sorted({item['site'] for item in site_files}),
        'files': len(site_files) + len(handover_files),
        'site_files': len(site_files),
        'handover_files': len(handover_files),
        'handovers': len(handovers),
        'subscriptions': len(adopted_by_subscription),
        'adopted': len(merged_records),
        'withdrawn': counts.get('撤下越界', 0),
        'withheld': counts.get('暂扣待移交', 0),
        'duplicates': counts.get('重复忽略', 0),
        'issues': len(issues),
        'errors': sum(1 for issue in issues if issue['级别'] == '错误'),
        'ready': bool(merged_records) and not any(issue['级别'] == '错误' for issue in issues),
    }
    return {'entries': entries, 'issues': issues, 'merged_records': merged_records, 'summary': summary}


def _snapshot_checksum(file_checksums):
    payload = json.dumps({'version': MERGE_VERSION, 'files': sorted(file_checksums)},
                         sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def ensure_state(connection):
    """Create the singleton pointer row if it is missing (safe under concurrency)."""
    if connection.scalar(select(ledger_state.c.id).where(ledger_state.c.id == 1)) is not None:
        return
    try:
        with connection.begin_nested():
            connection.execute(insert(ledger_state).values(id=1, snapshot_id=None, updated=time.time()))
    except IntegrityError:
        pass


def _lock_state(connection):
    ensure_state(connection)
    connection.scalar(select(ledger_state.c.id).where(ledger_state.c.id == 1).with_for_update())


def _ensure_file(connection, kind, site, name, records, checksum, owner, now):
    existing = connection.execute(select(ledger_files.c.id)
                                  .where(ledger_files.c.checksum == checksum)).scalar()
    if existing is not None:
        return existing, False
    key = connection.execute(insert(ledger_files).values(
        kind=kind, site=site, name=name, checksum=checksum, records=records,
        owner=owner, created=now)).inserted_primary_key[0]
    add_event(connection, owner, 'ledger_import', key, now)
    return key, True


def _file_inputs(files):
    site_files, handover_files = [], []
    for item in files:
        payload = {'name': item['name'], 'checksum': item['checksum'], 'records': item['records']}
        if item['kind'] == 'site':
            site_files.append({**payload, 'site': item['site']})
        else:
            handover_files.append(payload)
    return site_files, handover_files


def _effective_files(connection):
    """Latest ledger file per site, plus every handover file, in a stable order.

    Each site uploads its complete ledger, so a newer file for a site supersedes
    the earlier one; handover orders accumulate because each order is a separate
    instrument. Selecting by ascending file id keeps the set deterministic no
    matter what order the uploads arrived in.
    """
    latest_site = {}
    handovers = []
    for item in connection.execute(select(ledger_files).order_by(ledger_files.c.id)).mappings():
        if item['kind'] == 'site':
            latest_site[item['site']] = item
        else:
            handovers.append(item)
    ordered = sorted(latest_site.values(), key=lambda item: item['id']) + handovers
    return ordered


def rebuild(connection, owner, now):
    """Materialise the latest complete ledger inside the caller's transaction."""
    files = _effective_files(connection)
    site_files, handover_files = _file_inputs(files)
    result = merge(site_files, handover_files)
    checksum = _snapshot_checksum([item['checksum'] for item in files])
    existing = connection.execute(select(ledger_snapshots.c.id)
                                  .where(ledger_snapshots.c.checksum == checksum)).scalar()
    if existing is not None:
        connection.execute(update(ledger_state).where(ledger_state.c.id == 1)
                           .values(snapshot_id=existing, updated=now))
        return existing, False
    dataset_id = None
    if result['summary']['ready']:
        dataset_id, _ = save_revision_in(connection, '合并台账快照', result['merged_records'], owner, now)
    key = connection.execute(insert(ledger_snapshots).values(
        checksum=checksum, dataset_id=dataset_id,
        file_ids=sorted(item['id'] for item in files),
        entries=result['entries'], issues=result['issues'], summary=result['summary'],
        owner=owner, created=now)).inserted_primary_key[0]
    connection.execute(update(ledger_state).where(ledger_state.c.id == 1)
                       .values(snapshot_id=key, updated=now))
    add_event(connection, owner, 'ledger_snapshot', key, now)
    return key, True


def _ingest(kind, site, name, records, checksum, owner):
    name = name.strip()
    if not name:
        raise ValueError('文件名称不能为空。')
    now = time.time()
    with INGEST_LOCK:
        with engine.begin() as connection:
            _lock_state(connection)
            file_id, created_file = _ensure_file(connection, kind, site, name, records,
                                                 checksum, owner, now)
            snapshot_id, created_snapshot = rebuild(connection, owner, now)
            snapshot = connection.execute(select(ledger_snapshots)
                                          .where(ledger_snapshots.c.id == snapshot_id)).mappings().one()
    return {
        'file_id': file_id,
        'file_created': created_file,
        'snapshot_id': snapshot_id,
        'snapshot_created': created_snapshot,
        'dataset_id': snapshot['dataset_id'],
        'summary': snapshot['summary'],
        'issues': snapshot['issues'],
    }


def ingest_site(site, name, csv_text, owner):
    site = _clean_site(site)
    records = csv_records(csv_text)
    return _ingest('site', site, name, records, site_checksum(site, records), owner)


def ingest_handover(name, csv_text, owner):
    records = handover_records(csv_text)
    return _ingest('handover', None, name, records, handover_checksum(records), owner)


def current_snapshot():
    with engine.connect() as connection:
        snapshot_id = connection.scalar(select(ledger_state.c.snapshot_id)
                                        .where(ledger_state.c.id == 1))
        if snapshot_id is None:
            return None
        return dict(connection.execute(select(ledger_snapshots)
                                       .where(ledger_snapshots.c.id == snapshot_id)).mappings().one())


def list_files():
    with engine.connect() as connection:
        rows = connection.execute(select(ledger_files.c.id, ledger_files.c.kind,
                                         ledger_files.c.site, ledger_files.c.name,
                                         ledger_files.c.checksum, ledger_files.c.owner,
                                         ledger_files.c.created)
                                  .order_by(ledger_files.c.id.desc())).mappings().all()
    return [dict(row) for row in rows]
