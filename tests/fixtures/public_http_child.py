"""Loopback-only transport fixture; every exec installs its own audit guard."""
import os
from pathlib import Path
import sys
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
port=int(sys.argv[3] if sys.argv[1]=='--supervise' else sys.argv[3])
scratch=Path(sys.argv[4] if sys.argv[1]=='--supervise' else sys.argv[4]).resolve()
helper=Path(__file__).resolve()


def guard(event,args):
    if event in ('socket.connect','socket.connect_ex'):
        address=args[1]
        if not isinstance(address,tuple) or address[:2]!=('127.0.0.1',port):raise RuntimeError('EXTERNAL_NETWORK_FORBIDDEN')
    if event=='socket.getaddrinfo' and args[0]!='127.0.0.1':raise RuntimeError('DNS_FORBIDDEN')
    if event in ('socket.gethostbyname','socket.gethostbyaddr','socket.sendto','socket.sendmsg','socket.bind',
                 'os.system','os.posix_spawn','os.fork','os.forkpty') or event.startswith(('os.exec','os.spawn')):
        raise RuntimeError('NETWORK_OR_EXEC_FORBIDDEN')
    if event=='subprocess.Popen':
        command=args[1]
        if len(command)<3 or command[0]!=sys.executable or command[1]!='-B' or Path(command[2]).resolve()!=helper:
            raise RuntimeError('UNAPPROVED_CHILD_FORBIDDEN')
    if event=='open':
        path,mode,flags=args
        if not isinstance(path,int) and ((isinstance(mode,str) and any(x in mode for x in 'wax+'))
                or isinstance(flags,int) and flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)):
            target=Path(os.fsdecode(path)).resolve()
            if target!=scratch and scratch not in target.parents:raise RuntimeError('EXTERNAL_WRITE_FORBIDDEN')
    if event in ('os.remove','os.mkdir','os.rmdir','os.chmod','os.utime'):
        target=Path(os.fsdecode(args[0])).resolve()
        if target!=scratch and scratch not in target.parents:raise RuntimeError('EXTERNAL_WRITE_FORBIDDEN')


sys.addaudithook(guard)
if sys.argv[1]=='--supervise':
    from research import hunter_bybit_public as public
    from research.hunter_bybit_stream_store import Store
    public.worker_command=lambda url:[sys.executable,'-B',str(helper),url,str(os.getpid()),str(port),str(scratch),'normal']
    public.Client(Store.reader(Path(sys.argv[2]))).request('/v5/market/tickers',timeout=20)
else:
    import http.client
    import time
    from research import hunter_bybit_http_child as child
    mode=sys.argv[5]
    def local_open(opener, request, timeout):
        if mode=='dns_stall':time.sleep(60)
        connection=http.client.HTTPConnection('127.0.0.1',port,timeout=10)
        connection.request('GET','/fixture')
        response=connection.getresponse();response.code=response.status
        return response
    child.urllib.request.OpenerDirector.open=local_open
    if mode=='lost403':
        original=child.exchange
        def lost(url):
            def send(value):
                if value.get('kind')=='headers':return
                child.emit(value)
            return original(url,send)
        child.exchange=lost
    raise SystemExit(child.main())
