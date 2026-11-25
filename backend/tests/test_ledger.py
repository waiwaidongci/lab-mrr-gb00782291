import itertools

from src.services.ledger import handover_checksum, merge, site_checksum

TIERS = ('基础工位', '标准工位', '专用工位')
RATES = (120_000, 180_000, 260_000)


def record(subscription, tier, effective, mrr, status='active'):
    return {'account_id': subscription.rsplit('-', 1)[0], 'subscription_id': subscription,
            'tier': tier, 'effective_date': effective, 'status': status, 'mrr_minor': mrr}


def site_file(site, rows):
    return {'site': site, 'name': f'{site}.csv', 'checksum': site_checksum(site, rows), 'records': rows}


def handover_file(rows):
    return {'name': 'handover.csv', 'checksum': handover_checksum(rows), 'records': rows}


def handover(from_site, to_site, effective, subscription):
    return {'from_site': from_site, 'to_site': to_site, 'effective_date': effective,
            'subscription_id': subscription}


ORIGIN = [
    record('CW-01-01', '基础工位', '2026-09-01', 120_000),
    record('CW-01-01', '基础工位', '2026-10-02', 130_000),
    record('CW-01-01', '标准工位', '2026-10-08', 180_000),  # after handover -> withdrawn
]
RECEIVER = [
    record('CW-01-01', '基础工位', '2026-10-03', 125_000),  # before takeover -> withheld
    record('CW-01-01', '标准工位', '2026-10-09', 190_000),
]
THIRD = [record('CW-02-01', '专用工位', '2026-10-12', 260_000)]
ORDERS = [handover('甲', '乙', '2026-10-05', 'CW-01-01')]


def files():
    return [site_file('甲', ORIGIN), site_file('乙', RECEIVER), site_file('丙', THIRD)]


def results(entries):
    return {(entry['来源场地'], entry['订阅编号'], entry['生效日']): entry['采纳结果']
            for entry in entries if entry['记录类型'] == '生效记录'}


def test_order_of_arrival_does_not_change_ledger():
    base = merge(files(), [handover_file(ORDERS)])
    for site_order in itertools.permutations(files()):
        for order in itertools.permutations([handover_file(ORDERS)]):
            out = merge(list(site_order), list(order))
            assert out['merged_records'] == base['merged_records']
            assert out['entries'] == base['entries']


def test_receiver_withheld_until_handover_arrives():
    out = merge(files(), [])
    got = results(out['entries'])
    assert got[('乙', 'CW-01-01', '2026-10-03')] == '暂扣待移交'
    assert got[('乙', 'CW-01-01', '2026-10-09')] == '暂扣待移交'
    assert got[('甲', 'CW-01-01', '2026-10-08')] == '已采纳'
    assert out['summary']['withheld'] == 2


def test_late_handover_withdraws_and_restores():
    out = merge(files(), [handover_file(ORDERS)])
    got = results(out['entries'])
    assert got[('甲', 'CW-01-01', '2026-10-08')] == '撤下越界'
    assert got[('乙', 'CW-01-01', '2026-10-09')] == '已采纳'
    merged = [(row['effective_date'], row['mrr_minor'])
              for row in out['merged_records'] if row['subscription_id'] == 'CW-01-01']
    assert merged == [('2026-09-01', 120_000), ('2026-10-02', 130_000), ('2026-10-09', 190_000)]
    assert out['summary']['withdrawn'] == 1


def test_handover_without_reported_change_keeps_prior_state():
    origin = [record('CW-03-01', '标准工位', '2026-09-01', 180_000)]
    receiver = [record('CW-04-01', '基础工位', '2026-10-01', 120_000)]
    orders = [handover('甲', '乙', '2026-10-05', 'CW-03-01')]
    out = merge([site_file('甲', origin), site_file('乙', receiver)], [handover_file(orders)])
    carried = [row for row in out['merged_records'] if row['subscription_id'] == 'CW-03-01']
    assert carried == [{'account_id': 'CW-03', 'subscription_id': 'CW-03-01', 'tier': '标准工位',
                        'effective_date': '2026-09-01', 'status': 'active', 'mrr_minor': 180_000}]


def test_wildcard_handover_moves_every_subscription():
    origin = [record('CW-05-01', '基础工位', '2026-09-01', 120_000),
              record('CW-05-02', '标准工位', '2026-09-02', 180_000)]
    receiver = [record('CW-05-01', '基础工位', '2026-10-06', 120_000),
                record('CW-05-02', '标准工位', '2026-10-06', 180_000)]
    orders = [handover('甲', '乙', '2026-10-05', '*')]
    out = merge([site_file('甲', origin), site_file('乙', receiver)], [handover_file(orders)])
    got = results(out['entries'])
    assert got[('甲', 'CW-05-01', '2026-09-01')] == '已采纳'
    assert got[('乙', 'CW-05-01', '2026-10-06')] == '已采纳'
    # no origin records after the handover date, so nothing is out of bounds
    assert out['summary']['withdrawn'] == 0
    assert out['summary']['adopted'] == 4


def test_mismatched_handover_is_reported_and_ignored():
    orders = [handover('丙', '乙', '2026-10-05', 'CW-01-01')]
    out = merge(files(), [handover_file(orders)])
    assert any('移交方与当前归属' in issue['说明'] for issue in out['issues'])
    got = results(out['entries'])
    assert got[('乙', 'CW-01-01', '2026-10-09')] == '暂扣待移交'


def test_demo_ledgers_merge_back_to_flat_sample():
    from src.services.domain import sample_ledgers, sample_rows
    ledgers, orders = sample_ledgers()
    merged = merge([site_file(site, rows) for site, rows in ledgers.items()],
                   [handover_file(orders)])
    key = lambda row: (row['subscription_id'], row['effective_date'], row['tier'],
                       row['status'], row['mrr_minor'], row['account_id'])
    assert sorted(merged['merged_records'], key=key) == sorted(sample_rows(), key=key)
