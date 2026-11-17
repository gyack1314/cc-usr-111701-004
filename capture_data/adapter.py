import json
def decode(raw, manifest):
    doc=json.loads(raw.decode('utf-8'))
    for field in ('hub','topic','lease_id','lease_expires','signature'):
        if not doc.get(field):raise ValueError('回调字段缺失')
    lease=doc['lease_id']
    records=[{'kind':'lease','id':lease,'version':doc['lease_expires'],'refs':[],
              'payload':{'hub':doc['hub'],'topic':doc['topic']}},
             {'kind':'article','id':doc['topic'],'version':lease,'refs':[lease],
              'payload':{'callback_signature':doc['signature']}}]
    for entry in doc['entries']:
        records.append({'kind':'revision','id':entry['id'],'version':entry['revision'],
                        'refs':[doc['topic']],'payload':{'source':entry['source'],'deleted':entry['deleted']}})
    return {'protocol':manifest['protocol'],'collection':manifest['collection'],'records':records}
