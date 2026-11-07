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

VERSION='document-complexity.internal.v1'
MAX_BYTES=10*1024*1024
MAX_PAGES=40

def normalize(text):return ' '.join(text.split())

def inspect_structure(pdf_bytes):
    if not isinstance(pdf_bytes,bytes) or not 0<len(pdf_bytes)<=MAX_BYTES:
        raise ValueError('pdf_size_out_of_bounds')
    rows=[]
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
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
            reasons=[]
            if edges>=8:reasons.append('grid_structure')
            if column_gap:reasons.append('column_gap')
            if fonts>=4:reasons.append('font_diversity')
            if images and count:reasons.append('mixed_images')
            if count==0:reasons.append('no_text_layer' if images else 'empty_page')
            score=None if count==0 else round(min(1,.6*(edges>=8)+.5*column_gap+.1*(fonts>=4)+.1*image_fraction),4)
            rows.append({'page':number,'char_count':count,'char_density':round(count/(page.width*page.height),6),
                         'image_count':images,'image_area_fraction':round(image_fraction,4),
                         'edge_count':edges,'font_size_variants':fonts,'column_gap':column_gap,
                         'score':score,'reason_codes':reasons,'status':'unknown' if count==0 else 'observed',
                         'feature_source':'pdfplumber_page_layout_before_path_execution'})
    manual=any(p['score'] is None for p in rows)
    score=max((p['score'] for p in rows if p['score'] is not None),default=None)
    return {'feature_version':VERSION,'pdf_sha256':hashlib.sha256(pdf_bytes).hexdigest(),'pages':rows,
            'score':score,'calibrated':False,'strategy':'manual_review' if manual else ('structured' if score>=.35 else 'fast'),
            'reason_codes':sorted({r for p in rows for r in p['reason_codes']}),
            'unknown':manual,'OCR_available':False}

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

def process_adaptive(pdf_bytes):
    started=time.perf_counter()
    try:features=inspect_structure(pdf_bytes)
    except Exception as exc:
        return {'features':None,'strategy':'manual_review','success':False,'blocks':[],
                'error_code':'preflight_'+type(exc).__name__,'total_ms':round((time.perf_counter()-started)*1000,3)}
    preflight_ms=(time.perf_counter()-started)*1000
    if features['strategy']=='manual_review':
        result={'strategy':'manual_review','blocks':[],'success':False,'error_code':'OCR_or_manual_review_required','elapsed_ms':0}
    else:result=execute_path(pdf_bytes,features['strategy'])
    return {**result,'features':features,'preflight_ms':round(preflight_ms,3),
            'total_ms':round((time.perf_counter()-started)*1000,3)}
