"""Internal INT-08 prototype; prediction consumes PDF layout, never labels.

Preflight is real pdfplumber layout work, and its cost must be included when
comparing adaptive processing. No OCR/model/network or product API is exposed.
"""
from __future__ import annotations
import hashlib
import io
import time
import pdfplumber
from pypdf import PdfReader

VERSION='document-complexity.internal.v2'
MAX_BYTES=10*1024*1024
MAX_PAGES=40


def _aligned_gapped_rows(chars):
    """Count repeated three-or-more column starts using geometry only."""
    lines=[]
    for char in sorted((c for c in chars if c['text'].strip()),key=lambda c:(c['top'],c['x0'])):
        if not lines or abs(char['top']-lines[-1][0])>2:lines.append((char['top'],[char]))
        else:lines[-1][1].append(char)
    groups=[]
    for _,line in lines:
        line.sort(key=lambda c:c['x0']);starts=[line[0]['x0']];end=line[0]['x1']
        for char in line[1:]:
            if char['x0']-end>max(18,char['size']*2.5):starts.append(char['x0'])
            end=max(end,char['x1'])
        if len(starts)>=3:groups.append(starts)
    return max((sum(len(a)==len(b) and all(abs(x-y)<=4 for x,y in zip(a,b)) for b in groups) for a in groups),default=0)

def normalize(text):return ' '.join(text.split())

def _inspect_structure(pdf_bytes,*,_open_pdf=None):
    if not isinstance(pdf_bytes,bytes) or not 0<len(pdf_bytes)<=MAX_BYTES:
        raise ValueError('pdf_size_out_of_bounds')
    rows=[]
    from contextlib import nullcontext
    with (nullcontext(_open_pdf) if _open_pdf is not None else pdfplumber.open(io.BytesIO(pdf_bytes))) as pdf:
        if not 0<len(pdf.pages)<=MAX_PAGES:raise ValueError('pdf_pages_out_of_bounds')
        for number,page in enumerate(pdf.pages,1):
            chars=page.chars;count=len(chars);middle=page.width/2
            left=sum(c['x0']<middle-24 for c in chars)
            right=sum(c['x0']>middle+24 for c in chars)
            center=sum(abs((c['x0']+c['x1'])/2-middle)<12 for c in chars)
            edges=len(page.edges);images=len(page.images)
            image_area=sum(max(0,i['x1']-i['x0'])*max(0,i['bottom']-i['top']) for i in page.images)
            image_fraction=min(1,image_area/(page.width*page.height))
            fonts=len({(c['fontname'],round(c['size'],1)) for c in chars})
            column_gap=count>80 and min(left,right)/count>.2 and center/count<.015 and edges<8
            aligned_rows=_aligned_gapped_rows(chars) if edges<8 else 0
            reasons=[]
            if edges>=8:reasons.append('grid_structure')
            if column_gap:reasons.append('column_gap')
            if aligned_rows>=3:reasons.append('repeated_gapped_rows')
            if fonts>=4:reasons.append('font_diversity')
            if images and count:reasons.append('mixed_images')
            if count==0:reasons.append('no_text_layer' if images else 'empty_page')
            score=None if count==0 else round(min(1,.6*(edges>=8)+.5*column_gap+.5*(aligned_rows>=3)+.1*(fonts>=4)+.1*image_fraction),4)
            rows.append({'page':number,'char_count':count,'char_density':round(count/(page.width*page.height),6),
                         'image_count':images,'image_area_fraction':round(image_fraction,4),
                         'edge_count':edges,'font_size_variants':fonts,'column_gap':column_gap,
                         'aligned_gapped_rows':aligned_rows,'sparse_table_hint':aligned_rows>=3,
                         'score':score,'reason_codes':reasons,'status':'unknown' if count==0 else 'observed',
                         'feature_source':'pdfplumber_page_layout_before_path_execution'})
    manual=any(p['score'] is None for p in rows)
    score=max((p['score'] for p in rows if p['score'] is not None),default=None)
    return {'feature_version':VERSION,'pdf_sha256':hashlib.sha256(pdf_bytes).hexdigest(),'pages':rows,
            'score':score,'calibrated':False,'strategy':'manual_review' if manual else ('structured' if score>=.35 else 'fast'),
            'reason_codes':sorted({r for p in rows for r in p['reason_codes']}),
            'unknown':manual,'OCR_available':False}

def inspect_structure(pdf_bytes):
    return _inspect_structure(pdf_bytes)


def parse_fast(pdf_bytes):
    reader=PdfReader(io.BytesIO(pdf_bytes))
    if len(reader.pages)>MAX_PAGES:raise ValueError('pdf_pages_out_of_bounds')
    return [{'page':i,'text':normalize(page.extract_text() or ''),'kind':'page_text'} for i,page in enumerate(reader.pages,1)]

def parse_structured(pdf_bytes):
    """Real table extraction and column reading order via pdfplumber crops."""
    blocks=[]
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if len(pdf.pages)>MAX_PAGES:raise ValueError('pdf_pages_out_of_bounds')
        for i,page in enumerate(pdf.pages,1):
            tables=page.extract_tables()
            for table in tables:
                for row in table:
                    text=normalize(' '.join(cell or '' for cell in row))
                    if text:blocks.append({'page':i,'text':text,'kind':'table_row'})
            # Crop native two-column pages; grid tables keep page text plus rows.
            chars=page.chars;middle=page.width/2
            left=sum(c['x0']<middle-24 for c in chars);right=sum(c['x0']>middle+24 for c in chars)
            center=sum(abs((c['x0']+c['x1'])/2-middle)<12 for c in chars)
            columns=len(chars)>80 and min(left,right)/len(chars)>.2 and center/len(chars)<.015 and len(page.edges)<8
            areas=[page.crop((0,0,middle,page.height)),page.crop((middle,0,page.width,page.height))] if columns else [page]
            for area in areas:
                text=normalize(area.extract_text() or '')
                if text:blocks.append({'page':i,'text':text,'kind':'column' if columns else 'page_text'})
    return blocks

def execute_path(pdf_bytes,strategy):
    if not isinstance(pdf_bytes,bytes) or not 0<len(pdf_bytes)<=MAX_BYTES:raise ValueError('pdf_size_out_of_bounds')
    if strategy not in ('fast','structured'):raise ValueError('unsupported_strategy')
    start=time.perf_counter()
    try:
        blocks=(parse_fast if strategy=='fast' else parse_structured)(pdf_bytes)
        success=any(b['text'] for b in blocks)
        error=None if success else 'no_extractable_text'
    except Exception as exc:
        blocks=[];success=False;error=type(exc).__name__
    return {'strategy':strategy,'blocks':blocks,'success':success,'error_code':error,
            'elapsed_ms':round((time.perf_counter()-start)*1000,3)}

def select_available(features,available):
    if any(s not in ('fast','structured') for s in available) or len(available)!=len(set(available)):
        raise ValueError('unsupported_strategy')
    if features['unknown']:return None
    if features['strategy'] in available:return features['strategy']
    # Never downgrade a structural requirement to the known lossy fast path.
    return 'structured' if features['strategy']=='fast' and 'structured' in available else None


def _parse_reusing_layout(pdf,features):
    blocks=[]
    for page,observed in zip(pdf.pages,features['pages']):
        number=observed['page']
        if observed['edge_count']>=8:
            for table in page.extract_tables():
                for row in table:
                    text=normalize(' '.join(cell or '' for cell in row))
                    if text:blocks.append({'page':number,'text':text,'kind':'table_row'})
        middle=page.width/2
        areas=[page.crop((0,0,middle,page.height)),page.crop((middle,0,page.width,page.height))] if observed['column_gap'] else [page]
        for area in areas:
            text=normalize(area.extract_text() or '')
            if text:blocks.append({'page':number,'text':text,'kind':'column' if observed['column_gap'] else 'page_text'})
    return blocks


def process_adaptive(pdf_bytes,*,available=('fast','structured')):
    started=time.perf_counter();features=None;preflight_ms=0
    try:
        if not isinstance(pdf_bytes,bytes) or not 0<len(pdf_bytes)<=MAX_BYTES:raise ValueError('pdf_size_out_of_bounds')
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            features=_inspect_structure(pdf_bytes,_open_pdf=pdf)
            chosen=select_available(features,available)
            preflight_ms=(time.perf_counter()-started)*1000
            if chosen is None:
                result={'strategy':'manual_review','blocks':[],'success':False,
                        'error_code':'OCR_or_manual_review_required' if features['unknown'] else 'no_available_suitable_strategy','elapsed_ms':0}
            elif chosen=='fast':result=execute_path(pdf_bytes,'fast')
            else:
                parse_start=time.perf_counter()
                try:
                    blocks=_parse_reusing_layout(pdf,features);success=any(b['text'] for b in blocks)
                    result={'strategy':'structured','blocks':blocks,'success':success,
                            'error_code':None if success else 'no_extractable_text'}
                except Exception as exc:
                    result={'strategy':'structured','blocks':[],'success':False,'error_code':type(exc).__name__}
                result['elapsed_ms']=round((time.perf_counter()-parse_start)*1000,3)
    except Exception as exc:
        result={'strategy':'manual_review','success':False,'blocks':[],
                'error_code':'preflight_'+type(exc).__name__,'elapsed_ms':0}
        preflight_ms=(time.perf_counter()-started)*1000
    return {**result,'features':features,'preflight_ms':round(preflight_ms,3),
            'layout_reused':result['strategy']=='structured',
            'total_ms':round((time.perf_counter()-started)*1000,3)}
