"""通知只形成候选，不充当源站覆盖证明。"""
def assess(rows):
    pending=[r['id'] for r in rows if r['kind']=='revision' and not r['payload'].get('origin_reconciled')]
    untrusted=[r['id'] for r in rows if r['kind']=='article' and not r['payload'].get('signature_verified')]
    return {'publishable':not pending and not untrusted,
            'reasons':['通知未核签:'+x for x in untrusted]+['源站未回补:'+x for x in pending],
            'candidate_revisions':pending}
