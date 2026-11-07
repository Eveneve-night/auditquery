"""Original deterministic prompt_dev databases; never consumes scoring labels."""
import argparse,hashlib,json,sqlite3
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
ASSETS=ROOT/'data/public-business/v1'
MAINTENANCE='''CREATE TABLE asset(id INTEGER PRIMARY KEY, asset_name TEXT NOT NULL, site TEXT NOT NULL);
CREATE TABLE work_order(id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES asset(id), status TEXT NOT NULL, planned_hours NUMERIC NOT NULL);
CREATE TABLE maintenance_event(id INTEGER PRIMARY KEY, work_order_id INTEGER NOT NULL REFERENCES work_order(id), duration_hours NUMERIC NOT NULL, event_date TEXT NOT NULL);
'''
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def sha(path):
    data=path.read_bytes()
    if path.suffix in ('.json','.sql','.md'):data=data.replace(b'\r\n',b'\n')
    return hashlib.sha256(data).hexdigest()
def create_database(ddl,rows,path):
    if path.exists():raise FileExistsError('output_database_exists')
    with closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA foreign_keys=ON');db.executescript(ddl)
        db.execute('PRAGMA defer_foreign_keys=ON')
        for table,values in rows.items():
            for value in values:
                columns=list(value)
                db.execute('INSERT INTO "'+table+'" ('+','.join('"'+c+'"' for c in columns)+') VALUES ('+','.join('?' for _ in columns)+')',[value[c] for c in columns])
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
        db.commit()
    return path
def order_rows(ddl):
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript(ddl);tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        rows={}
        for table in tables:
            columns=db.execute('PRAGMA table_info("'+table+'")').fetchall();values=[]
            for n in range(1,4):
                value={}
                for _,name,kind,*_ in columns:
                    if name=='id' or name.endswith('_id'):v=n
                    elif name.endswith('_amount'):v=10*n*(n+1)
                    elif name=='unit_price':v=10*n
                    elif 'quantity' in name:v=100-n if name=='on_hand_quantity' else n+1
                    elif kind=='TEXT':v='2026-10-0'+str(n) if name.endswith(('_at','_date','_time')) else name+'_'+str(n)
                    else:v=n
                    value[name]=v
                values.append(value)
            rows[table]=values
    return rows
def prepare():
    ASSETS.mkdir(parents=True,exist_ok=True)
    ddl=(ROOT/'data/schema-linking/order_to_cash/v1/schema.sql').read_text(encoding='utf-8')
    (ASSETS/'orders.sql').write_text(ddl,encoding='utf-8',newline='\n')
    (ASSETS/'maintenance.sql').write_text(MAINTENANCE,encoding='utf-8',newline='\n')
    write(ASSETS/'orders-seed.json',order_rows(ddl))
    write(ASSETS/'maintenance-seed.json',{
        'asset':[{'id':1,'asset_name':'Synthetic Pump','site':'plant_A'},{'id':2,'asset_name':'Synthetic Motor','site':'plant_B'}],
        'work_order':[{'id':1,'asset_id':1,'status':'open','planned_hours':4},{'id':2,'asset_id':2,'status':'closed','planned_hours':2}],
        'maintenance_event':[{'id':1,'work_order_id':2,'duration_hours':2,'event_date':'2026-10-01'}]})
    entries=[]
    for name in ('orders','maintenance'):
        files=[name+'.sql',name+'-seed.json'];entries.append({'domain':name,'files':{f:sha(ASSETS/f) for f in files}})
    write(ASSETS/'manifest.json',{'version':'1.0.0','split':'prompt_dev','origin':'original_project_synthetic','entries':entries,
                               'order_DDL_source':'data/schema-linking/order_to_cash/v1/schema.sql','customer_data':False,'human_cost_hours':None})
def build(output):
    output.mkdir(parents=True,exist_ok=True);manifest=json.loads((ASSETS/'manifest.json').read_text(encoding='utf-8'));result=[]
    for item in manifest['entries']:
        name=item['domain']
        for path,expected in item['files'].items():
            if sha(ASSETS/path)!=expected:raise ValueError('asset_hash_mismatch')
        db=create_database((ASSETS/(name+'.sql')).read_text(encoding='utf-8'),json.loads((ASSETS/(name+'-seed.json')).read_text(encoding='utf-8')),output/(name+'.sqlite3'))
        with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as connection:
            tables=[r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            counts={t:connection.execute('SELECT count(*) FROM "'+t+'"').fetchone()[0] for t in tables}
            assert connection.execute('PRAGMA foreign_key_check').fetchall()==[]
            try:connection.execute('CREATE TABLE forbidden(id INTEGER)')
            except sqlite3.OperationalError:pass
            else:raise AssertionError('readonly_write_accepted')
        result.append({'domain':name,'database_sha256':sha(db),'table_rows':counts,'readonly_write_rejected':True})
    return result
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--prepare',action='store_true');p.add_argument('--output',type=Path);a=p.parse_args()
    if a.prepare:prepare()
    if a.output:print(json.dumps(build(a.output.resolve()),ensure_ascii=False))
