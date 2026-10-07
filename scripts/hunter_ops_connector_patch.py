"""CAS patch for the existing read-only connector; auth policy is untouched."""
import argparse
import ast
import hashlib
import os
from pathlib import Path
import py_compile


def patch(source):
    tree=ast.parse(source)
    lines=source.splitlines(keepends=True)
    replacements=[]
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_OPS_UNITS' for t in node.targets):
            old=ast.get_source_segment(source,node)
            if 'hunter-market-stream.service' not in old:
                units=ast.literal_eval(node.value)+('hunter-market-stream.service',)
                replacements.append((node.lineno-1,node.end_lineno,'_OPS_UNITS = '+repr(units)+'\n'))
        if isinstance(node,ast.FunctionDef) and node.name=='deployment_status':
            old=''.join(lines[node.lineno-1:node.end_lineno])
            if 'enrich_deployment' in old: raise ValueError('ALREADY_PATCHED')
            old=old.replace('    return {','    base = {',1)
            old+='\n    from hunter_ops_health import read_health, enrich_deployment\n    try:\n        health = read_health()\n    except (OSError, ValueError, KeyError, TypeError):\n        health = None\n    return enrich_deployment(base, health)\n'
            replacements.append((node.lineno-1,node.end_lineno,old))
        if isinstance(node,ast.FunctionDef) and node.name=='runtime_file':
            old=''.join(lines[node.lineno-1:node.end_lineno])
            needle='    from pathlib import Path\n'
            insertion='''    if path in ('hunter-fast-watch-health.json', '/run/hunter-fast-watch/health.json'):
        import json
        from hunter_ops_health import read_health
        return {'path': path, 'content': json.dumps(read_health())}
'''
            if needle not in old: raise ValueError('UNEXPECTED_RUNTIME_READER')
            replacements.append((node.lineno-1,node.end_lineno,old.replace(needle,needle+insertion,1)))
    if len(replacements)!=3: raise ValueError('UNEXPECTED_CONNECTOR_STRUCTURE')
    for start,end,new in sorted(replacements,reverse=True): lines[start:end]=[new]
    updated=''.join(lines);ast.parse(updated)
    return updated


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server',required=True,type=Path)
    parser.add_argument('--expected-sha256',required=True)
    args=parser.parse_args();original=args.server.read_bytes()
    if hashlib.sha256(original).hexdigest()!=args.expected_sha256:
        raise ValueError('CONNECTOR_CAS_MISMATCH')
    changed=patch(original.decode()).encode()
    backup=args.server.with_name('server.py.before-fast-health-'+args.expected_sha256[:12])
    with backup.open('xb') as f: f.write(original)
    backup.chmod(0o600)
    temporary=args.server.with_name('server.py.fast-health.tmp')
    with temporary.open('xb') as f: f.write(changed)
    temporary.chmod(args.server.stat().st_mode & 0o777)
    py_compile.compile(str(temporary),doraise=True)
    if hashlib.sha256(args.server.read_bytes()).hexdigest()!=args.expected_sha256:
        temporary.unlink();raise ValueError('CONNECTOR_CAS_MISMATCH')
    os.replace(temporary,args.server)
    print('PATCHED',hashlib.sha256(changed).hexdigest(),'BACKUP',str(backup))


if __name__=='__main__': main()
