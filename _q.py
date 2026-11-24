import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, r'C:/Users/ZYT/Documents/deepseek-harness/default-workspace/plugins/dsh-jm/lib')
import jm_pick
r = jm_pick.cmd_pick_download({'id': '1454244,509318', 'per_book_timeout': 1200, 'max_pages': 0})
print('RESULT ok=%s' % r['ok'])
for x in r['downloaded']: print('DL|%s|%d|%.1f' % (x['id'], x['pages'], x['size_mb']))
for x in r.get('skipped') or []: print('SKIP|%s|%d' % (x['id'], x['pages']))
for x in r['failed']: print('FAIL|%s|%s' % (x['id'], str(x['error'])[:80]))