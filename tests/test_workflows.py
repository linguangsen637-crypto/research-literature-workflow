import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from research_os.core import SafetyError, canonical, digest
from research_os.metadata import arxiv_id, candidates, resolve, safe_url
from research_os.library_ops import zotero_record, patch_plan, ZoteroWriter, equivalent
from research_os.papers import validate_analysis
from research_os.maintenance import search, inspect
from test_safety import item


class MetadataTests(unittest.TestCase):
    def test_arxiv_versions(self):
        self.assertEqual(arxiv_id('https://arxiv.org/pdf/1904.05866v2.pdf'), ('1904.05866', 2))
        self.assertEqual(arxiv_id('arxiv:2106.10893'), ('2106.10893', None))

    def test_invalid_arxiv(self):
        with self.assertRaises(SafetyError): arxiv_id('../../private')

    def test_unsafe_url(self):
        for url in ['http://arxiv.org/pdf/x', 'https://127.0.0.1/x', 'https://arxiv.org.evil/x',
                    'https://user:secret@arxiv.org/pdf/x']:
            with self.assertRaises(SafetyError): safe_url(url)

    def test_resolve_crossref_is_source_attributed(self):
        payload = {'message': {'DOI': '10.1234/example', 'title': ['Example'], 'author': [{'family':'Author'}],
                               'published': {'date-parts': [[2026]]}, 'type': 'proceedings-article'}}
        with patch('research_os.metadata.fetch', return_value=json.dumps(payload).encode()):
            result = resolve('https://doi.org/10.1234/EXAMPLE')
        self.assertEqual(result['fields']['year'], 2026)
        self.assertIn('sha256', result['sources'][0])

    def test_metadata_identity_mismatch_rejected(self):
        with patch('research_os.metadata.fetch', return_value=b'{"message":{"DOI":"10.9999/wrong"}}'):
            with self.assertRaises(SafetyError): resolve('10.1234/example')

    def test_title_match_is_only_candidate(self):
        result = candidates({'fields': {'title': 'Example'}}, [item()])
        self.assertEqual(result[0]['action'], 'review-existing')
        self.assertEqual(result[0]['reasons'], ['normalized-title-only-not-proof'])

    def test_preprint_doi_not_promoted_to_published_identity(self):
        f={'title':'Paper','authors':[{'name':'Author'}],'year':2026,'publication_type':'preprint',
           'doi':'10.1234/published','arxiv_id':'2601.12345','arxiv_version':2}
        result=zotero_record({'fields': f},'COLLECT1')
        self.assertNotIn('DOI', result)
        self.assertIn('Related published DOI', result['extra'])


class FakeClient:
    base='http://127.0.0.1:23119/api/'
    server_id='server'
    prefix='users/42/'
    config={'library':{'type':'user','id':42}}
    def collection(self): return {'key':'COLLECT1'}
    def paper(self,key): return item()


class WritesTests(unittest.TestCase):
    def test_wire_attachment_preserves_linkmode_before_filename(self):
        from unittest.mock import MagicMock
        c=FakeClient();c.opener=MagicMock()
        c.opener.open.return_value.__enter__.return_value.read.return_value=b'{}'
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict('os.environ', {'ZOTERO_LOCAL_API_KEY':'fixture-only'}):
                w=ZoteroWriter(c,Path(tmp)/'state',Path(tmp)/'backup')
                w.request('POST','users/42/items',[{'linkMode':'imported_file','filename':'paper.pdf'}])
                wire=c.opener.open.call_args.args[0].data.decode()
                self.assertLess(wire.index('linkMode'),wire.index('filename'))
    def test_server_tag_reordering_is_equivalent(self):
        self.assertTrue(equivalent('tags', [{'tag':'b'}, {'tag':'a','type':1}],
                                   [{'tag':'a','type':1}, {'tag':'b','type':0}]))
    def test_tags_preserve_existing(self):
        c=FakeClient()
        old=item();old['data']['tags']=[{'tag':'manual-tag'}]
        c.paper=lambda key:old
        plan=patch_plan(c,old['key'],{'tags':[{'tag':'hand-model'}]})
        self.assertEqual({t['tag'] for t in plan['changes']['tags']},{'manual-tag','hand-model'})

    def test_reject_collection_overwrite(self):
        with self.assertRaises(SafetyError): patch_plan(FakeClient(),'ABCDEFGH',{'collections':[]})

    def test_stale_version_never_calls_write(self):
        c=FakeClient();plan=patch_plan(c,'ABCDEFGH',{'title':'New'})
        plan['version']=0
        with tempfile.TemporaryDirectory() as tmp:
            w=ZoteroWriter(c,Path(tmp)/'state',Path(tmp)/'backup')
            with patch.object(w,'request') as request:
                with self.assertRaises(SafetyError): w.apply(plan,digest(canonical(plan)))
                request.assert_not_called()


class EvidenceTests(unittest.TestCase):
    def test_pdf_control_characters_are_recorded(self):
        from research_os.papers import parse_pdf
        from types import SimpleNamespace
        xml=b'<html><page><line xMin="0" yMin="0" xMax="10" yMax="12"><word>1. Introduction</word></line><line xMin="0" yMin="20" xMax="100" yMax="30"><word>'+b'reliable text '*15+b'\x02</word></line></page></html>'
        with tempfile.TemporaryDirectory() as tmp:
            pdf=Path(tmp)/'source.pdf';pdf.write_bytes(b'%PDF-fixture')
            with patch('research_os.papers.subprocess.run', side_effect=[SimpleNamespace(stderr='Poppler fixture\n'),SimpleNamespace(returncode=0,stdout=xml)]):
                result=parse_pdf(pdf)
            self.assertEqual(result['xml_control_characters_omitted'],1)
            self.assertEqual(result['sections'][0]['title'],'1. Introduction')
    def request(self):
        return {'task_id':'task','paper_identity':{'library_type':'user','library_id':42,'item_key':'ABCDEFGH'},
                'sources':[{'source_id':'source','pdf_sha256':'a'*64,'version':'v1','location':'paper.pdf'}],
                'requirements':{'source_pages':[{'source_id':'source','pdf_page':1,'text':'Measured evidence is on this page.'}]},
                'results':[], 'verification_status':'Unverified'}
    def output(self):
        r=self.request()
        r['results']=[{'claim':'Measured evidence','kind':'paper_fact','verification_status':'Unverified',
                      'evidence':[{'source_id':'source','pdf_page':1,'section':'Abstract','location':'Measured evidence'}]}]
        return r
    def test_quote_and_page_validated(self):
        self.assertEqual(validate_analysis(self.output(),self.request())['result_count'],1)
    def test_fabricated_quote_rejected(self):
        r=self.output();r['results'][0]['evidence'][0]['location']='Fabricated result'
        with self.assertRaises(SafetyError):validate_analysis(r,self.request())
    def test_model_cannot_self_verify(self):
        r=self.output();r['results'][0]['verification_status']='Verified'
        with self.assertRaises(SafetyError):validate_analysis(r,self.request())
    def test_fact_without_evidence_rejected(self):
        r=self.output();r['results'][0]['evidence']=[]
        with self.assertRaises(SafetyError):validate_analysis(r,self.request())
    def test_metadata_search_not_claimed_as_fulltext(self):
        self.assertEqual(search([item()], 'Example')[0]['evidence'],'metadata-only; full text not read')
    def test_maintenance_duplicates_only_reported(self):
        a=item();b=copy.deepcopy(a);b['key']='BCDEFGHJ'
        with tempfile.TemporaryDirectory() as tmp:
            result=inspect([a,b],Path(tmp)/'vault',Path(tmp)/'state',{'type':'user','id':42},{})
            self.assertTrue(any(f['kind']=='duplicate-doi-candidate' for f in result['findings']))
            self.assertEqual(result['repairs_applied'],[])


if __name__=='__main__':unittest.main()
