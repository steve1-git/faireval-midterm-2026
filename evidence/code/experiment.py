"""Frozen jobs, durable execution records and format-specific scoring."""
from pathlib import Path
from decimal import Decimal,InvalidOperation
import hashlib,json,random,re,sqlite3,time,os
from protocol import render_extension_prompt,sampling_seed,parse_extension_score,aggregate_selected

def digest(obj):return hashlib.sha256(json.dumps(obj,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
def save(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf8');os.replace(tmp,path)
def read(path):return json.loads(Path(path).read_text(encoding='utf8'))
def load(root):
    root=Path(root);cfg=read(root/'configs/experiment.json');mapping=read(root/'configs/sample_mapping.json');orientation=read(root/'configs/orientations.json')
    def lines(name):return [json.loads(s) for s in (root/'data'/name).read_text(encoding='utf8').splitlines()]
    questions,a,b=lines('question.jsonl'),lines('answer_gpt35.jsonl'),lines('answer_vicuna.jsonl')
    labels=(root/'data/human.txt').read_text(encoding='utf8').splitlines();items=[]
    for q,x,y,h in zip(questions,a,b,labels):
        assert q['question_id']==x['question_id']==y['question_id']
        items.append({'id':f'FairEval-{q["question_id"]:03d}','dataset':'FairEval','subset':'calibration' if q['question_id'] in cfg['data']['FairEval']['calibration_ids'] else 'test','question':q['text'],'X':x['text'],'Y':y['text'],'gold':{'CHATGPT':'X','VICUNA13B':'Y','TIE':'TIE'}[h.strip()]})
    for row in lines('external70.jsonl'):
        items.append({'id':row['external_id'],'dataset':'LLMBar','subset':row['subset'],'question':row['input'],'X':row['output_1'],'Y':row['output_2'],'gold':'X' if row['label']==1 else 'Y'})
    assert len(items)==150 and set(x['id'] for x in items)==set(orientation['question_ids_in_bit_order'])
    return cfg,mapping,orientation,{x['id']:x for x in items}

VANILLA_REQUEST=('Please first output a single line containing only two values indicating the scores for Assistant 1 and 2, respectively. '
 'The two scores are separated by a space. In the subsequent line, please provide a comprehensive explanation of your evaluation, '
 'avoiding any potential bias and ensuring that the order in which the responses were presented does not affect your judgment.')
def messages(item,layout,kind):
    system,user=render_extension_prompt(item['question'],item['X'],item['Y'],layout,item['dataset'])
    if kind=='vanilla':
        start=user.index('    Please first provide a comprehensive explanation')
        user=user[:start]+VANILLA_REQUEST+'\n'
    return [{'role':'system','content':system},{'role':'user','content':user}]
def jobs_for(model,cfg,mapping,items):
    slug=model['model'].split('/')[-1];jobs=[];canonical,reverse=cfg['standard_BPC_layouts']
    def job(qid,kind,block,layout,draw):
        seed=sampling_seed(model['revision'],qid,block,layout,draw if kind!='vanilla' else 0)
        gen=cfg['generation']
        return {'job_id':f'{slug}|{qid}|{kind}|b{block}|{layout}|d{draw}', 'model':model['model'],'revision':model['revision'],'question_id':qid,'kind':kind,'block':block,'layout':layout,'draw':draw,'seed':seed,
                'sampling':{'temperature':gen['temperature_sampling'] if kind=='mc' else 0.0,'top_p':gen['top_p'],'logical_top_k':gen['top_k'],'repetition_penalty':gen['repetition_penalty'],'max_tokens':gen['max_new_tokens']}}
    ids=list(items);random.Random('v31-question-order|0').shuffle(ids)
    for qid in ids:
        for kind in ['vanilla','ec']:
            for layout in [canonical,reverse]:jobs.append(job(qid,kind,0,layout,1))
    for block in range(1,4):
        ids=list(items);random.Random(f'v31-question-order|{block}').shuffle(ids)
        for qid in ids:
            for key in mapping['frozen_shuffled_layout_draw_order_per_block'][str(block)]:
                layout,draw=key.split('#draw');jobs.append(job(qid,'mc',block,layout,int(draw)))
    assert len(jobs)==11400 and len({x['job_id'] for x in jobs})==11400
    return jobs
def score_output(job,raw,layouts):
    layout=layouts[job['layout']];truncated=raw['finish_reason']=='length'
    if job['kind']!='vanilla':return parse_extension_score(raw['text'],layout,truncated)
    reasons=[];values=None
    first=raw['text'].strip().splitlines()[0] if raw['text'].strip() else ''
    pair=re.fullmatch(r'\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*',first)
    if pair:
        values={1:Decimal(pair[1]),2:Decimal(pair[2])}
        if any(not x.is_finite() or not 1<=x<=10 for x in values.values()):reasons.append('score_outside_1_to_10')
    else:reasons.append('invalid_leading_two_score_format')
    if truncated:reasons.append('truncated')
    return {'valid':not reasons,'reasons':reasons,'scores':{identity:str(values[label]) for identity,label in layout['labels'].items()} if values else None,'assigned_S':layout['S'],'actual_S':None,'order_compliant':None,'truncated':truncated}

class Journal:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(exist_ok=True,parents=True)
        self.db=sqlite3.connect(self.path);self.db.row_factory=sqlite3.Row
        self.db.execute('pragma journal_mode=WAL');self.db.execute('pragma synchronous=FULL')
        self.db.execute('create table if not exists jobs (ordinal integer,job_id text primary key,spec_hash text,spec text,state text,raw text,parsed text,updated real)')
        self.db.execute('create table if not exists attempts (event_id integer primary key,job_id text,event text,payload text,time real)')
        self.db.commit()
    def initialize(self,jobs):
        existing={r['job_id']:r['spec_hash'] for r in self.db.execute('select job_id,spec_hash from jobs')}
        for i,job in enumerate(jobs):
            if job['job_id'] in existing:assert existing[job['job_id']]==digest(job),'Frozen task changed'
            else:self.db.execute('insert into jobs values (?,?,?,?,?,?,?,?)',(i,job['job_id'],digest(job),json.dumps(job,ensure_ascii=False),'pending_execution',None,None,time.time()))
        self.db.commit();assert self.db.execute('select count(*) from jobs').fetchone()[0]==len(jobs)
    def event(self,job_id,event,payload):
        self.db.execute('insert into attempts(job_id,event,payload,time) values (?,?,?,?)',(job_id,event,json.dumps(payload,ensure_ascii=False),time.time()));self.db.commit()
    def state(self,job_id):return self.db.execute('select state from jobs where job_id=?',(job_id,)).fetchone()[0]
    def save_output(self,job,raw):
        assert raw['finish_reason'] in ['stop','length']
        assert self.state(job['job_id'])=='pending_execution'
        self.db.execute('update jobs set raw=?,state=?,updated=? where job_id=?',(json.dumps(raw,ensure_ascii=False),'pending_scoring',time.time(),job['job_id']));self.db.commit()
    def score_saved(self,layouts):
        for row in self.db.execute("select job_id,spec,raw from jobs where state='pending_scoring'").fetchall():
            job=json.loads(row['spec']);raw=json.loads(row['raw'])
            try:parsed=score_output(job,raw,layouts)
            except Exception as e:
                self.event(row['job_id'],'parser_implementation_error',{'type':type(e).__name__,'message':str(e)});raise
            self.db.execute('update jobs set parsed=?,state=?,updated=? where job_id=?',(json.dumps(parsed,ensure_ascii=False),'completed_valid' if parsed['valid'] else 'completed_invalid',time.time(),row['job_id']));self.db.commit()
    def records(self):
        return {row['job_id']:{'spec':json.loads(row['spec']),'state':row['state'],'raw':json.loads(row['raw']) if row['raw'] else None,'parsed':json.loads(row['parsed']) if row['parsed'] else None} for row in self.db.execute('select * from jobs order by ordinal')}
    def counts(self):return dict(self.db.execute('select state,count(*) from jobs group by state').fetchall())
    def export(self,folder):
        folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
        rows=self.db.execute('select ordinal,job_id,spec,state,raw,parsed from jobs where raw is not null order by ordinal').fetchall()
        for start in range(0,len(rows),500):
            path=folder/f'part_{start//500:03d}.jsonl';tmp=path.with_suffix('.tmp')
            with tmp.open('w',encoding='utf8') as stream:
                for row in rows[start:start+500]:stream.write(json.dumps({'job_id':row['job_id'],'spec':json.loads(row['spec']),'state':row['state'],'raw':json.loads(row['raw']),'parsed':json.loads(row['parsed']) if row['parsed'] else None},ensure_ascii=False)+'\n')
            os.replace(tmp,path)
        events=[dict(row) for row in self.db.execute('select * from attempts order by event_id')]
        with (folder/'attempts.jsonl').open('w',encoding='utf8') as stream:
            for event in events:stream.write(json.dumps(event,ensure_ascii=False)+'\n')
