import argparse, json, os
from dotenv import load_dotenv
from .core import PROMPTS, connect, plan, run, summary, report

def main():
    load_dotenv()
    p=argparse.ArgumentParser(description='Cloud video model benchmark (dry-run by default)')
    p.add_argument('--db',default='results/benchmark.sqlite3')
    sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('plan');a.add_argument('--models',nargs='+',choices=['minimax','flux','wan'],default=['minimax','flux','wan']);a.add_argument('--scenes',nargs='+',choices=list(PROMPTS),default=['T01','T03','T07']);a.add_argument('--repeats',type=int,default=1);a.add_argument('--duration',type=int,default=5)
    a=sub.add_parser('run');a.add_argument('--live',action='store_true');a.add_argument('--budget',type=float,default=None);a.add_argument('--limit',type=int,default=0);a.add_argument('--poll-interval',type=int,default=15);a.add_argument('--job-id',help='Run or preview only this exact SQLite job ID')
    sub.add_parser('status')
    a=sub.add_parser('report');a.add_argument('--output',default='results/report.html')
    a=sub.add_parser('attach');a.add_argument('id');a.add_argument('remote_id');a.add_argument('--confirm',action='store_true')
    a=sub.add_parser('score');a.add_argument('id');a.add_argument('value',type=float)
    args=p.parse_args();db=connect(args.db)
    if args.command=='plan':
        if args.repeats<1: p.error('repeats must be positive')
        print('Added',plan(db,args.models,args.scenes,args.repeats,args.duration),'jobs')
    elif args.command=='run':
        budget=args.budget if args.budget is not None else float(os.getenv('BENCHMARK_BUDGET_USD','20'))
        if budget<0 or args.poll_interval<1 or args.limit<0: p.error('Invalid budget, interval or limit')
        try:
            run(db,args.live,budget,args.limit,args.poll_interval,job_id=args.job_id)
        except ValueError as e:
            p.error(str(e))
    elif args.command=='status': print(json.dumps(summary(db),indent=2))
    elif args.command=='report': print(report(db,args.output))
    elif args.command=='attach':
        if not args.confirm: p.error('attach requires --confirm after checking provider dashboard')
        row=db.execute('SELECT * FROM jobs WHERE id=?',(args.id,)).fetchone()
        if not row or row['status']!='submitting' or row['remote_id']: p.error('Job must be ambiguous submitting state without remote_id')
        db.execute("UPDATE jobs SET status='submitted',remote_id=?,submitted=? WHERE id=?",(args.remote_id,__import__('time').time(),args.id));db.commit()
    elif args.command=='score':
        if not 1<=args.value<=5: p.error('Score must be between 1 and 5')
        if not db.execute('SELECT 1 FROM jobs WHERE id=?',(args.id,)).fetchone(): p.error('Unknown job')
        db.execute('UPDATE jobs SET score=? WHERE id=?',(args.value,args.id));db.commit()

if __name__=='__main__':main()
