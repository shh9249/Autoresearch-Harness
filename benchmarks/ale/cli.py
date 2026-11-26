import argparse
import json
from pathlib import Path
from adapter import evaluate

def main():
    parser=argparse.ArgumentParser(description='AHC026 native task adapter; no API or search')
    parser.add_argument('--request',type=Path,required=True)
    args=parser.parse_args()
    request=json.loads(args.request.read_text(encoding='utf-8-sig'))
    # All paths in request resolve from the invocation working directory.
    result=evaluate(request)
    # ASCII escapes keep Unicode paths intact across Windows shell code pages.
    print(json.dumps(result,ensure_ascii=True,indent=2))
    return 0 if result['status']=='success' else 2

if __name__=='__main__': raise SystemExit(main())
