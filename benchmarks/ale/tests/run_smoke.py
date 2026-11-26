from pathlib import Path
import sys,json,time
from concurrent.futures import ThreadPoolExecutor
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from adapter import evaluate

def main():
    fixtures=ROOT/'tests'/'generated'; fixtures.mkdir(exist_ok=True)
    for name,content in {
        'compile_error.cpp':'this is not valid C++\n',
        'runtime_error.py':'raise RuntimeError("intentional smoke failure")\n',
        'timeout.py':'import time\ntime.sleep(60)\n',
        'invalid.py':'print("1 0")\n',
        'overflow.py':'import sys\nsys.stdout.write("X"*100000)\n',
        'timeout_children.py':'import subprocess,sys,time\np=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\nprint(p.pid,flush=True)\ntime.sleep(60)\n',
    }.items(): (fixtures/name).write_text(content,encoding='utf8')
    def request(cid,source,lang='python',**config):
        return {'task_id':'ahc026','candidate_id':cid,'source_path':str(source),'language':lang,'public_seed':0,
                'config':{'run_root':str(ROOT/'test_runs'),**config}}
    cases=[
      ('valid_cpp',request('valid_cpp',ROOT/'samples'/'valid.cpp','cpp'),'success'),
      ('valid_python',request('valid_python',ROOT/'samples'/'valid.py'),'success'),
      ('compile_error',request('compile_error',fixtures/'compile_error.cpp','cpp'),'compile_error'),
      ('runtime_error',request('runtime_error',fixtures/'runtime_error.py'),'runtime_error'),
      ('timeout',request('timeout',fixtures/'timeout.py',run_timeout_seconds=.3),'timeout'),
      ('invalid_output',request('invalid',fixtures/'invalid.py'),'invalid_output'),
      ('output_limit',request('overflow',fixtures/'overflow.py',output_limit_bytes=4096),'output_limit_exceeded'),
      ('missing_scorer',request('missing_scorer',ROOT/'samples'/'valid.py',scorer=str(fixtures/'does-not-exist.exe')),'infrastructure_error'),
      ('missing_compiler',request('missing_compiler',ROOT/'samples'/'valid.cpp','cpp',compiler=str(fixtures/'does-not-exist.exe')),'infrastructure_error'),
      ('timeout_children',request('timeout_children',fixtures/'timeout_children.py',run_timeout_seconds=.6),'timeout'),
    ]
    records=[]
    for name,req,expected in cases:
        result=evaluate(req)
        assert result['status']==expected,(name,result)
        if expected!='success': assert result['score'] is None
        else: assert result['score']==7945
        records.append({'case':name,'expected':expected,'actual':result['status'],'stage':result['stage'],'score':result['score'],'result_path':result['result_path']})
        if name=='timeout_children':
            import ctypes,os
            if os.name=='nt':
                from ctypes import wintypes as w
                pid=int(Path(result['phases']['run']['stdout_path']).read_text().strip())
                k=ctypes.WinDLL('kernel32',use_last_error=True)
                k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
                k.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];k.WaitForSingleObject.restype=w.DWORD
                k.CloseHandle.argtypes=[w.HANDLE]
                handle=k.OpenProcess(0x100000,False,pid)
                exited=not handle or k.WaitForSingleObject(handle,1000)==0
                if handle:k.CloseHandle(handle)
                assert exited,'Child process survived timeout cleanup'
                records[-1]['child_process_exited']=True
        print(name,result['status'],result['score'],flush=True)
    req=request('same_id_parallel',ROOT/'samples'/'valid.py')
    with ThreadPoolExecutor(max_workers=2) as pool:
        parallel=list(pool.map(evaluate,[req,req]))
    assert len({r['run_id'] for r in parallel})==2
    assert all(r['status']=='success' and r['score']==7945 for r in parallel)
    records.append({'case':'parallel_same_candidate_id','actual':'success','result_paths':[r['result_path'] for r in parallel]})
    req=request('unsupported',ROOT/'samples'/'valid.py');req['task_id']='ahc025'
    unsupported=evaluate(req); assert unsupported['status']=='unsupported_task'
    records.append({'case':'unsupported_025','actual':unsupported['status'],'result_path':unsupported['result_path']})
    req=request('explicit_input',ROOT/'samples'/'valid.py'); req.pop('public_seed')
    req['input_path']=str(Path(parallel[0]['run_dir'])/'input.txt');req['instance_id']='public_seed0_copy'
    explicit=evaluate(req);assert explicit['status']=='success' and explicit['score']==7945
    records.append({'case':'explicit_input','actual':explicit['status'],'result_path':explicit['result_path']})
    report={'passed':True,'cases':records,'llm_calls':0,'note':'Native CPU smoke only; not optimization results or official Docker reproduction'}
    (ROOT/'tests'/'smoke_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')

if __name__=='__main__': main()
