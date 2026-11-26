"""AHC026 native task adapter. No model, search, budget or retry policy."""
from __future__ import annotations
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = 1

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def workspace_root():
    for p in Path(__file__).resolve().parents:
        if (p / 'bench_setup_2026-11-25').is_dir():
            return p
    return Path.cwd()

def windows_job(proc):
    """Kill-on-close job, attached immediately after spawn (not a security sandbox)."""
    if os.name != 'nt':
        return None
    from ctypes import wintypes as w
    class BASIC(ctypes.Structure):
        _fields_ = [('ProcessUserTimeLimit',ctypes.c_int64),('PerJobUserTimeLimit',ctypes.c_int64),('LimitFlags',w.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',w.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',w.DWORD),('SchedulingClass',w.DWORD)]
    class IO(ctypes.Structure):
        _fields_=[(s,ctypes.c_uint64) for s in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
    class EXT(ctypes.Structure):
        _fields_=[('BasicLimitInformation',BASIC),('IoInfo',IO),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR]; k.CreateJobObjectW.restype=w.HANDLE
    k.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]; k.SetInformationJobObject.restype=w.BOOL
    k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]; k.AssignProcessToJobObject.restype=w.BOOL
    k.CloseHandle.argtypes=[w.HANDLE]; k.CloseHandle.restype=w.BOOL
    handle=k.CreateJobObjectW(None,None)
    if not handle: return None
    info=EXT(); info.BasicLimitInformation.LimitFlags=0x2000
    if not k.SetInformationJobObject(handle,9,ctypes.byref(info),ctypes.sizeof(info)) or not k.AssignProcessToJobObject(handle,int(proc._handle)):
        k.CloseHandle(handle); return None
    return (k,handle)

def stop_tree(proc,job):
    if job:
        job[0].CloseHandle(job[1]); return
    if proc.poll() is not None: return
    if os.name=='nt':
        killer=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'/'taskkill.exe'
        try:
            subprocess.run([str(killer),'/PID',str(proc.pid),'/T','/F'],capture_output=True,timeout=10)
        except (OSError,subprocess.TimeoutExpired):
            if proc.poll() is None: proc.kill()
    else:
        try: os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError: pass

def run_process(command,cwd,stage,timeout,limit,stdin_path=None,env=None):
    """Bound each stdout/stderr file, record lifecycle, never retry."""
    cwd=Path(cwd); out=cwd/(stage+'.stdout'); err=cwd/(stage+'.stderr')
    record={'command':[str(x) for x in command], 'stdout_path':str(out),'stderr_path':str(err),
            'timeout_seconds':timeout,'output_limit_bytes_per_stream':limit,'exit_code':None,
            'timed_out':False,'output_limit_exceeded':False,'spawn_error':None}
    started=time.monotonic(); event=threading.Event(); read_errors=[]; counts={}
    proc=None; job=None
    inp=open(stdin_path,'rb') if stdin_path else open(os.devnull,'rb')
    def drain(pipe,path,label):
        total=0; written=0
        try:
            with open(path,'wb') as f:
                while True:
                    chunk=pipe.read(4096)
                    if not chunk: break
                    total+=len(chunk)
                    allowed=max(0,min(len(chunk),limit-written))
                    if allowed: f.write(chunk[:allowed]); written+=allowed
                    if total>limit: event.set()
        except (OSError,ValueError) as e:
            read_errors.append(type(e).__name__)
        finally:
            counts[label]=total
    try:
        options={'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
        proc=subprocess.Popen([str(x) for x in command],cwd=cwd,stdin=inp,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,**options)
        record['pid']=proc.pid
        job=windows_job(proc)
        record['process_tree_control']='windows_job' if job else ('taskkill_best_effort' if os.name=='nt' else 'process_group')
        threads=[threading.Thread(target=drain,args=(pipe,path,label),daemon=True) for pipe,path,label in [(proc.stdout,out,'stdout'),(proc.stderr,err,'stderr')]]
        for thread in threads: thread.start()
        while proc.poll() is None:
            if event.is_set(): record['output_limit_exceeded']=True; break
            if time.monotonic()-started>timeout: record['timed_out']=True; break
            time.sleep(.01)
        stop_tree(proc,job); job=None
        try: record['exit_code']=proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            record['cleanup_error']='process_did_not_exit'; proc.kill()
        for thread in threads: thread.join(timeout=3)
        record['capture_incomplete']=any(t.is_alive() for t in threads)
        record['output_limit_exceeded'] |= event.is_set()
        record['observed_output_bytes']=counts
        if read_errors: record['capture_errors']=read_errors
    except OSError as e:
        record['spawn_error']={'type':type(e).__name__,'message':str(e)}
    finally:
        if proc is not None and (job or proc.poll() is None): stop_tree(proc,job)
        inp.close(); record['elapsed_seconds']=round(time.monotonic()-started,6)
    return record

class AdapterFailure(Exception):
    def __init__(self,status,code,message):
        self.status,self.code,self.message=status,code,message

def evaluate(request):
    config=request.get('config',{}); root=workspace_root()
    run_root=Path(config.get('run_root',root/'autoresearch_rebuild'/'benchmarks'/'ale'/'runs')).resolve()
    run_dir=run_root/uuid.uuid4().hex; run_dir.mkdir(parents=True)
    result={'schema_version':SCHEMA_VERSION,'run_id':run_dir.name,'task_id':request.get('task_id'),
        'candidate_id':request.get('candidate_id'),'public_seed':request.get('public_seed'),
        'language':request.get('language'),'status':'infrastructure_error','stage':'preflight',
        'score':None,'score_direction':'maximize' if request.get('task_id')=='ahc026' else None,'source_sha256':None,'input_sha256':None,
        'started_at':datetime.now(timezone.utc).isoformat(),'run_dir':str(run_dir),'phases':{},
        'artifacts':{},'events':[],'retry_count':0,'budget_accounting':'not_decided_by_adapter'}
    start=time.monotonic()
    def fail(status,code,message): raise AdapterFailure(status,code,message)
    def tool(name,default):
        p=Path(config.get(name,default)).resolve()
        if not p.is_file(): fail('infrastructure_error','missing_'+name,str(p))
        return p
    def phase(name,command,stdin=None):
        result['stage']=name
        seconds=float(config.get(name+'_timeout_seconds',{'generate':20,'compile':60,'run':5,'score':20}[name]))
        limit=int(config.get('output_limit_bytes',2*1024*1024))
        if seconds<=0 or limit<1: fail('request_error','invalid_limits','Timeout and output limit must be positive')
        record=run_process(command,run_dir,name,seconds,limit,stdin,env)
        result['phases'][name]=record
        result['events'].append({'type':'phase_finished','phase':name,'exit_code':record['exit_code'],'elapsed_seconds':record['elapsed_seconds']})
        if record['spawn_error']: fail('infrastructure_error',name+'_spawn_error',record['spawn_error']['message'])
        if record.get('capture_incomplete') or record.get('capture_errors') or record.get('cleanup_error'): fail('infrastructure_error',name+'_capture_or_cleanup_error','See phase record')
        if record['timed_out']: fail('timeout' if name in ('run','compile') else 'infrastructure_error',name+'_timeout','Stage exceeded configured wall timeout')
        if record['output_limit_exceeded']: fail('output_limit_exceeded' if name in ('run','compile') else 'infrastructure_error',name+'_output_limit','Captured stream exceeded configured bytes')
        if record['exit_code']!=0:
            fail({'compile':'compile_error','run':'runtime_error'}.get(name,'infrastructure_error'),name+'_nonzero_exit','See stderr artifact')
        return record
    env=os.environ.copy()
    env.pop('DEEPSEEK_API_KEY', None)
    try:
        if request.get('task_id')!='ahc026': fail('unsupported_task','only_ahc026','AHC025/027 are not implemented by this adapter')
        if not isinstance(request.get('candidate_id'),str) or not request['candidate_id']: fail('request_error','candidate_id_required','Nonempty candidate ID required')
        language=request.get('language')
        if language not in ('cpp','python'): fail('request_error','unsupported_language','Use cpp or python')
        source=Path(request.get('source_path','')).resolve()
        if not source.is_file(): fail('request_error','missing_source',str(source))
        if source.stat().st_size>int(config.get('source_limit_bytes',1024*1024)): fail('request_error','source_too_large','Source exceeds configured size limit')
        copied=run_dir/('candidate.cpp' if language=='cpp' else 'candidate.py'); shutil.copyfile(source,copied)
        result['source_sha256']=digest(copied); result['artifacts']['source']=str(copied)
        tools_dir=root/'bench_setup_2026-11-25'/'ale'/'native'/'ahc026'/'tools_x86_64-pc-windows-gnu'
        scorer=tool('scorer',tools_dir/'vis.exe')
        result['toolchain']={'scorer':str(scorer),'scorer_sha256':digest(scorer)}
        input_path=run_dir/'input.txt'
        if request.get('input_path') is not None:
            if request.get('public_seed') is not None: fail('request_error','ambiguous_instance','Provide input_path OR public_seed')
            original=Path(request['input_path']).resolve()
            if not original.is_file(): fail('request_error','missing_input',str(original))
            if original.stat().st_size>1024*1024: fail('request_error','input_too_large','Input capped at 1 MiB')
            shutil.copyfile(original,input_path); result['instance_id']=request.get('instance_id',original.name)
        else:
            seed=request.get('public_seed',0)
            if type(seed) is not int or not 0<=seed<50: fail('request_error','not_public_seed','AHC026 public seeds are integers 0..49')
            result['public_seed']=seed; result['instance_id']='public_seed_'+str(seed)
            generator=tool('generator',tools_dir/'gen.exe'); result['toolchain']['generator']=str(generator); result['toolchain']['generator_sha256']=digest(generator)
            (run_dir/'seeds.txt').write_text(str(seed)+'\n',encoding='ascii')
            phase('generate',[generator,'seeds.txt'])
            generated=run_dir/'in'/'0000.txt'
            if not generated.is_file(): fail('infrastructure_error','generator_missing_input','Generator did not produce in/0000.txt')
            shutil.copyfile(generated,input_path)
        try:
            tokens=list(map(int,input_path.read_text(encoding='utf8').split())); n,m=tokens[:2]
            assert (n,m)==(200,10) and len(tokens)==n+2 and sorted(tokens[2:])==list(range(1,n+1))
        except (ValueError,AssertionError): fail('request_error','invalid_instance','AHC026 requires N=200 M=10 and permutation 1..200')
        result['input_sha256']=digest(input_path); result['artifacts']['input']=str(input_path)
        if language=='cpp':
            matches=sorted((root/'bench_setup_2026-11-25'/'tools').glob('llvm-mingw-*/bin/clang++.exe'))
            compiler=tool('compiler',matches[-1] if matches else root/'missing-clang++.exe')
            env['PATH']=str(compiler.parent)+os.pathsep+env.get('PATH','')
            exe=run_dir/'candidate.exe'
            result['toolchain']['compiler']=str(compiler)
            phase('compile',[compiler,'-std=c++17','-O2',copied,'-o',exe])
            command=[exe]
        else:
            python=tool('python',sys.executable); result['toolchain']['python']=str(python)
            phase('compile',[python,'-I','-m','py_compile',copied])
            command=[python,'-I','-B',copied]
        execution=phase('run',command,input_path)
        if digest(input_path)!=result['input_sha256']:
            fail('invalid_output','input_modified_by_candidate','Candidate changed the saved benchmark input')
        output=Path(execution['stdout_path']); result['artifacts']['candidate_output']=str(output)
        score_run=phase('score',[scorer,input_path,output])
        text=Path(score_run['stdout_path']).read_text(encoding='utf8',errors='replace')
        score_lines=re.findall(r'^Score = (\d+)\s*$',text,re.M)
        if len(score_lines)!=1: fail('infrastructure_error','scorer_protocol_error','Expected one official Score line')
        raw=int(score_lines[0]); result['official_reported_score']=raw
        diagnostics=re.sub(r'^Score = \d+\s*$','',text,flags=re.M).strip()
        if raw==0 or diagnostics: fail('invalid_output','official_rejection',diagnostics or 'Official scorer rejected output (score zero)')
        result.update(status='success',stage='complete',score=raw)
        if (run_dir/'vis.html').is_file(): result['artifacts']['visualization']=str(run_dir/'vis.html')
    except AdapterFailure as e:
        result.update(status=e.status,error={'code':e.code,'message':e.message})
    except Exception as e:
        result.update(status='infrastructure_error',error={'code':'adapter_exception','message':type(e).__name__+': '+str(e)})
    result['elapsed_seconds']=round(time.monotonic()-start,6)
    result['finished_at']=datetime.now(timezone.utc).isoformat()
    result['result_path']=str(run_dir/'result.json')
    tmp=run_dir/'result.tmp.json'; tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8'); tmp.replace(run_dir/'result.json')
    return result
