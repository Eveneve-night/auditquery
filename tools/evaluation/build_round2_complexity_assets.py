"""Deterministic small original synthetic PDFs; labels separate from metadata."""
import hashlib
import io
import json
from pathlib import Path
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader
from PIL import Image,ImageDraw,ImageFont
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'tests/fixtures/document-complexity/v1'

def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')

def build():
    (OUT/'pdfs').mkdir(parents=True,exist_ok=True)
    entries=[];labels=[]
    families=['prose','columns','ruled_table','sparse_table','mixed']
    for group,family in enumerate(families):
        for index in range(6):
            code=f'{group+1}{index+1:02d}';doc=f'{family}-{index+1:02d}';path=OUT/'pdfs'/f'{doc}.pdf'
            c=Canvas(str(path),pagesize=(612,792),invariant=1,pageCompression=1)
            c.setTitle('Public synthetic '+doc);c.setAuthor('AuditQuery synthetic development fixtures')
            c.setFont('Helvetica-Bold',14);c.drawString(45,750,'Synthetic operations record '+code)
            facts=[];quantity=11+index*7+group;pages=1
            if family=='prose':
                c.setFont('Helvetica',11)
                lines=[f'Kit P{code} requires {quantity} units before dispatch.',
                       f'The return desk accepts kit P{code} only after inspection.']
                for j,line in enumerate(lines):c.drawString(45,700-j*22,line)
                for j in range(16+index):c.drawString(45,630-j*20,f'Procedure {j+1}: check the local manifest and record the event.')
                facts.append(dict(query=f'Kit P{code} requires',quote=lines[0],page=1))
                if index%3==0:
                    c.showPage();pages=2;c.setFont('Helvetica',11)
                    line=f'Extension P{code} permits storage for {quantity+2} days.'
                    c.drawString(45,720,line);facts.append(dict(query=f'Extension P{code} storage',quote=line,page=2))
            elif family=='columns':
                c.setFont('Helvetica',9)
                left=[f'Kit C{code} requires',f'{quantity} units before dispatch.']+[f'Left procedure {j}: retain the local log.' for j in range(24)]
                right=[f'Other region C{code} is distinct.','Do not substitute that region.']+[f'Right note {j}: verify the approval list.' for j in range(24)]
                for j,(a,b) in enumerate(zip(left,right)):
                    c.drawString(45,710-j*21,a);c.drawString(333,710-j*21,b)
                facts.append(dict(query=f'Kit C{code} requires units',quote=' '.join(left[:2]),page=1))
            else:
                top=665;row_height=26;xs=[45,215,370,565]
                if family!='sparse_table':
                    c.setLineWidth(.6)
                    for x in xs:c.line(x,top,x,top-row_height*9)
                    for j in range(10):c.line(xs[0],top-j*row_height,xs[-1],top-j*row_height)
                columns=[['Item']+[f'Stock {code}-{j}' for j in range(8)],
                         ['Quantity']+[str(quantity+j) for j in range(8)],['Unit']+['units']*8]
                c.setFont('Helvetica',10)
                # Column-major writing order is real PDF content order, not a
                # post-hoc text corruption. Layout rows still render normally.
                for x,values in zip(xs,columns):
                    for j,value in enumerate(values):c.drawString(x+8,top-18-j*row_height,value)
                if family=='mixed':
                    image=Image.new('RGB',(180,50),'#e5edf5');draw=ImageDraw.Draw(image)
                    draw.rectangle((8,10,145,38),fill='#44749d')
                    c.drawImage(ImageReader(image),45,310,width=180,height=50)
                    c.setFont('Times-Italic',11);c.drawString(45,290,'Illustration is structural context, not answer evidence.')
                facts.append(dict(query=f'Stock {code}-0 quantity units',quote=f'Stock {code}-0 {quantity} units',page=1))
            c.setFont('Helvetica',8);c.drawString(45,30,'Public synthetic development document; no real customer data.')
            c.save()
            entries.append(dict(document_id=doc,path=f'pdfs/{doc}.pdf',family_id=family,
                                structure_type=family,split='prompt_dev',pages=pages,
                                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                generation_parameters={'serial':code,'quantity':quantity,'generator':'build_round2_complexity_assets.v1'}))
            labels.append(dict(document_id=doc,questions=facts,scan_boundary=False))
    for index in range(2):
        doc=f'scan-boundary-{index+1:02d}';path=OUT/'pdfs'/f'{doc}.pdf'
        image=Image.new('RGB',(720,920),'white');draw=ImageDraw.Draw(image)
        # Pure geometry preserves the no-text/OCR boundary without external fonts.
        draw.rectangle((50,70,550,120),fill='black')
        draw.ellipse((50,150,150+index*40,250),fill='black')
        c=Canvas(str(path),pagesize=(612,792),invariant=1);c.drawImage(ImageReader(image),0,0,width=612,height=792);c.save()
        entries.append(dict(document_id=doc,path=f'pdfs/{doc}.pdf',family_id='image_only',structure_type='image_only',split='prompt_dev',pages=1,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),generation_parameters={'generator':'image_only_boundary.v1'}))
        labels.append(dict(document_id=doc,questions=[],scan_boundary=True))
    write(OUT/'manifest.json',dict(version='1.0.0',source='original_project_synthetic',license='MIT',split='prompt_dev',document_count=len(entries),family_count=6,independent_blind_samples=0,documents=entries))
    write(OUT/'scoring.json',labels)

if __name__=='__main__':build()
