"""Synthetic model files / metadata only. Never commit personal keys or workflows."""
import json, sys, threading, time, unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import webui, comfy_metadata as metadata

class AccessTests(unittest.TestCase):
    def api(self,key=''):
        a=webui.Api.__new__(webui.Api);a.cfg={'api_key':key,'hash_threads':1};a.dl=Mock();a.rp_rows=[];a.model_rows=[];a._rp_launch_lock=threading.RLock();a._rp_state={'running':False};a._rp_stop_ev=threading.Event();a._rp_pause_ev=threading.Event();a._new_api=Mock(return_value=Mock());return a
    def test_empty_key_blocks_all_new_online_entries_without_side_effects(self):
        a=self.api('  ')
        for name,args in [('parse_urls',[['https://civitai.red/models/1']]),('_enqueue_one',['https://civitai.red/models/1']),('dl_submit_url',['https://civitai.red/models/1']),('dl_enqueue_url',['https://civitai.red/models/1']),('mm_check_updates',[]),('mm_check_update',[]),('mm_update_models',[]),('rp_identify_model',['missing'])]:
            with self.subTest(name=name):
                r=getattr(a,name)(*args);self.assertFalse(r['started']);self.assertTrue(r['requires_api_key'])
        self.assertFalse(a.rp_start());self.assertFalse(a.dl_action('start_all'));a.dl.add_task.assert_not_called();a._new_api.assert_not_called()
    def test_safety_controls_remain_available_without_key(self):
        a=self.api();a.dl.tasks=[];self.assertTrue(a.dl_action('pause_all'));a.dl.save_tasks.return_value=True;self.assertTrue(a.dl_action('save'))
    def test_existing_key_not_validated_or_disclosed_by_gate(self):
        a=self.api('synthetic-key');a.api=Mock();a.dl_enqueue_url=Mock(return_value={'started':True});a._notify_ext_download_started=Mock()
        r=a.dl_submit_url('https://civitai.red/models/1');self.assertTrue(r['ok']);self.assertNotIn('synthetic-key',json.dumps(r));a.dl_enqueue_url.assert_called_once()
    def test_bridge_call_convention_two_positional_args_with_subdir(self):
        """browser_bridge 的 /api/download 一律 fn(url, dest_subdir) 两参调用；少一个参数就是整条扩展下载链 500。

        回归 v2.6.30 实测故障：注册从 dl_enqueue_url_sync（两参）换成 dl_submit_url（当时只收一参）后，
        扩展「发送当前页」与归档的 sendModelToSoftware 全部返回
        "下载启动失败: Api.dl_submit_url() takes 2 positional arguments but 3 were given"。
        """
        import inspect
        for name in ('dl_submit_url','dl_enqueue_url'):
            with self.subTest(name=name):
                params=[p for p in inspect.signature(getattr(webui.Api,name)).parameters.values() if p.name!='self']
                self.assertGreaterEqual(len(params),2,f'{name} 必须收 (url, dest_subdir)')
                self.assertTrue(all(p.default is not inspect.Parameter.empty for p in params[1:]),f'{name} 第二个参数必须有默认值（老调用方仍传单参）')
        a=self.api('synthetic-key');a.api=Mock();a.dl_enqueue_url=Mock(return_value={'started':True});a._notify_ext_download_started=Mock()
        r=a.dl_submit_url('https://civitai.red/models/1','Demo Model_v1.0')
        self.assertTrue(r['ok']);a.dl_enqueue_url.assert_called_once_with('https://civitai.red/models/1','Demo Model_v1.0')
        b=self.api('synthetic-key');b.api=Mock();b._notify_ext_download_started=Mock()
        with patch.object(webui.Api,'_enqueue_one') as enq:
            enq.return_value={'url':'u','ok':True,'msg':''}
            b.dl_enqueue_url('https://civitai.red/models/1','Demo Model_v1.0')
            for _ in range(200):
                if enq.call_args:break
                time.sleep(.01)
            self.assertTrue(enq.call_args,f'后台入队未执行，dest_subdir 无从透传')
            self.assertEqual(enq.call_args.kwargs.get('dest_subdir'),'Demo Model_v1.0')
    def wait(self,a):
        for _ in range(100):
            if not a._rp_state['running']:return
            time.sleep(.01)
        self.fail('worker did not stop')
    def result(self,*a,**k):return {'sha256':'a'*64,'found':False,'error':'fixture failure'}
    def test_failed_retry_scope_excludes_success_waiting_and_unselected(self):
        with TemporaryDirectory() as d:
            rows=[]
            for n,st in [('failed','失败'),('success','成功'),('waiting','等待'),('unselected','未收录')]:
                p=Path(d)/(n+'.safetensors');p.write_bytes(b'model');rows.append({'path':str(p),'status':st,'sha':'','model':'','version':''})
            a=self.api('synthetic-key');a.rp_rows=rows
            with patch('reverse_parse.reverse_by_hash',side_effect=self.result) as call:
                self.assertTrue(a.rp_start([r['path'] for r in rows[:3]],True));self.wait(a);self.assertEqual([c.args[0] for c in call.call_args_list],[rows[0]['path']])
    def test_empty_failed_scope_does_not_launch(self):
        a=self.api('synthetic-key');a.rp_rows=[{'path':'success','status':'成功'}];self.assertFalse(a.rp_start(['success'],True));self.assertFalse(a.rp_start([]));a._new_api.assert_not_called()
    def test_selected_remove_is_list_only_and_preserves_active(self):
        with TemporaryDirectory() as d:
            p=Path(d)/'a.safetensors';p.write_bytes(b'model')
            a=self.api();a.rp_rows=[{'path':str(p)},{'path':'busy'},{'path':'other'}];a._rp_state={'running':True,'active_paths':['busy']}
            r=a.rp_remove_selected([str(p),'busy']);self.assertEqual(r['removed'],1);self.assertEqual(r['skipped'],1);self.assertEqual(p.read_bytes(),b'model');self.assertEqual([r['path'] for r in a.rp_rows],['busy','other'])

class LoraManagerTests(unittest.TestCase):
    def graph(self,value):return {'1':{'class_type':'Lora Loader (LoraManager)','inputs':{'loras':value}}}
    def test_nested_value_enabled_and_disabled(self):
        r=metadata.extract_model_references(self.graph({'__value__':[{'name':'demo_on','active':True,'strength':'0.40'},{'name':'demo_off','active':False,'selected':True}]}));self.assertEqual(r['models'],['demo_on']);self.assertEqual(r['disabled_models'],['demo_off'])
    def test_json_serialized_value_and_no_extension(self):
        r=metadata.extract_model_references(self.graph(json.dumps({'__value__':[{'name':'demo.2','active':True}]})));self.assertEqual(r['models'],['demo.2'])
    def test_ui_only_stack(self):
        w={'nodes':[{'id':1,'type':'Lora Loader (LoraManager)','widgets_values':[{'version':2},'unused',[{'name':'demo_a','active':True},{'name':'demo_b','active':False}]]}]}
        r=metadata.extract_model_references(workflow=w);self.assertEqual(r['models'],['demo_a']);self.assertEqual(r['disabled_models'],['demo_b'])
    def test_api_prevents_stale_ui_activation(self):
        w={'nodes':[{'id':1,'type':'Lora Loader (LoraManager)','widgets_values':[[{'name':'demo_off','active':True}]]}]}
        r=metadata.extract_model_references(self.graph({'__value__':[{'name':'demo_off','active':False}]}),w);self.assertEqual(r['models'],[]);self.assertEqual(r['disabled_models'],['demo_off'])
    def test_note_and_prompt_strings_are_not_models(self):
        w={'nodes':[{'id':1,'type':'Note','widgets_values':['see https://example.invalid/demo.safetensors']},{'id':2,'type':'CLIPTextEncode','widgets_values':['a landscape, demo.safetensors']}]}
        self.assertEqual(metadata.extract_model_references(workflow=w)['models'],[])
    def test_bypassed_ui_loader_is_not_active(self):
        w={'nodes':[{'id':1,'mode':4,'type':'LoraLoader','widgets_values':['demo.safetensors',1,1]}]}
        r=metadata.extract_model_references(workflow=w);self.assertEqual(r['models'],[]);self.assertEqual(r['disabled_models'],['demo.safetensors'])
    def test_malformed_stack_does_not_crash(self):
        for value in [None,5,'{broken',{'__value__':None},[None,5],{'__value__':[{'name':5}]}]:
            self.assertEqual(metadata.extract_model_references(self.graph(value))['models'],[])
    def test_local_match_extensionless_dotted_names_multiple_roots(self):
        with TemporaryDirectory() as d:
            one=Path(d)/'one';two=Path(d)/'two';one.mkdir();two.mkdir();p=two/'demo.2.safetensors';p.write_bytes(b'model')
            a=webui.Api.__new__(webui.Api);a.cfg={'models_dirs':[str(one),str(two)]}
            r=json.loads(a.workflow_model_matches(['demo.2']));self.assertTrue(r[0]['local']);self.assertEqual(Path(r[0]['path']),p)

if __name__=='__main__':unittest.main()
