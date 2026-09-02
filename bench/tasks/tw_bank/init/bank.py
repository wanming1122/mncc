"""银行账户（实现正确，任务是为它写测试，不要修改本文件）。"""


class Account:
    def __init__(self, owner):
        self.owner = owner
        self._balance = 0

    @property
    def balance(self):
        return self._balance

    def deposit(self, amount):
        if amount <= 0:
            raise ValueError("存款金额必须为正")
        self._balance += amount

    def withdraw(self, amount):
        if amount <= 0:
            raise ValueError("取款金额必须为正")
        if amount > self._balance:
            raise ValueError("余额不足")
        self._balance -= amount
