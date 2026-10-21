"""A bounded continuous pool: drain admitted work on halt, never discard receipts."""
import asyncio


async def parallel_map(items, call, concurrency, *, halt=lambda value:False, throttle=lambda value:False):
    if concurrency<1:raise ValueError('concurrency must be positive')
    iterator=iter(items);pending={};stopped=False;limit=concurrency
    def fill():
        while not stopped and len(pending)<limit:
            try:item=next(iterator)
            except StopIteration:break
            pending[asyncio.create_task(call(item))]=item
    fill()
    try:
        while pending:
            done,_=await asyncio.wait(pending,return_when=asyncio.FIRST_COMPLETED)
            outcomes=[]
            for task in done:
                item=pending.pop(task)
                try:
                    value=task.result();outcomes.append((item,value,None))
                    if halt(value):stopped=True
                    if throttle(value):limit=max(1,limit//2)
                except Exception as error:
                    stopped=True;outcomes.append((item,None,error))
            for outcome in outcomes:yield outcome
            fill()
    finally:
        # Cancellation must not abandon a paid subprocess started via to_thread.
        if pending:await asyncio.gather(*pending,return_exceptions=True)
