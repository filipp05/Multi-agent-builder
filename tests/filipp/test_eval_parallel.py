import asyncio
from evals.filipp.parallel import parallel_map


def test_slow_first_does_not_block_next_job_and_limit_holds():
    async def run():
        release=asyncio.Event();started=[];active=0;maximum=0
        async def job(i):
            nonlocal active,maximum
            active+=1;maximum=max(maximum,active);started.append(i)
            if i==0:await release.wait()
            if i==2:release.set()
            await asyncio.sleep(.001);active-=1;return i
        got=[x async for x in parallel_map(range(5),job,2)]
        assert sorted(x[1] for x in got)==list(range(5))
        assert maximum==2 and started[:3]==[0,1,2]
    asyncio.run(asyncio.wait_for(run(),2))


def test_halt_drains_inflight_without_starting_more():
    async def run():
        started=[]
        async def job(i):
            started.append(i)
            if i:await asyncio.sleep(.02)
            return i
        got=[x async for x in parallel_map(range(8),job,2,halt=lambda x:x==0)]
        assert started==[0,1]
        assert len(got)==2
    asyncio.run(run())


def test_failure_drains_other_receipt_and_stops_queue():
    async def run():
        started=[]
        async def job(i):
            started.append(i)
            if i==0:raise ValueError('fail')
            await asyncio.sleep(.01);return 'receipt'
        got=[x async for x in parallel_map(range(8),job,2)]
        assert started==[0,1]
        assert sum(x[2] is not None for x in got)==1
        assert any(x[1]=='receipt' for x in got)
    asyncio.run(run())
