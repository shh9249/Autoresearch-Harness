n,m=map(int,input().split())
stacks=[list(map(int,input().split())) for _ in range(m)]
for value in range(1,n+1):
    i=next(i for i,s in enumerate(stacks) if value in s)
    j=stacks[i].index(value)
    if j+1<len(stacks[i]):
        target=(i+1)%m
        print(stacks[i][j+1],target+1)
        stacks[target].extend(stacks[i][j+1:])
        del stacks[i][j+1:]
    print(value,0)
    stacks[i].pop()
