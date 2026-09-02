实现 retry.py 里的 @retry(max_attempts=3, backoff=0.1) 装饰器：

- 被装饰函数抛出异常时自动重试，直到成功或用完 max_attempts（把初次尝试计入次数）
- 每次重试之前 sleep backoff 秒（用标准库 time.sleep）
- 用尽次数后抛出最后一次的异常
- 参数、返回值正确透传；不要吞掉 KeyboardInterrupt

改完用 python 命令验证（例如一个前两次抛异常、第三次成功的函数）。