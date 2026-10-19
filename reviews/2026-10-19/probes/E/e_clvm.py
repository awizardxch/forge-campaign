import random
from flpcr.clvm import sha256tree, int_to_atom
from chia_wallet_sdk import Clvm
try:
    from clvm import SExp
    from clvm.casts import int_to_bytes
except Exception as e:
    int_to_bytes=None; print("no clvm pkg", e)
clvm = Clvm()
vals=[0,1,-1,127,128,-128,-129,255,256,-256,0x7fff,0x8000,-0x8000,-0x8001,2**64,-(2**64),2**255,-(2**255)-1,10**40]
vals += [random.randint(-2**300,2**300) for _ in range(2000)]
bad=0
for v in vals:
    a=int_to_atom(v)
    if int_to_bytes and int_to_bytes(v)!=a: bad+=1; print("pkg mismatch",v)
    if bytes(clvm.int(v).tree_hash()) != sha256tree(v) if hasattr(clvm,'int') else False: bad+=1; print("sdk mismatch", v)
# nested structures vs sdk
def to_sdk(x):
    if isinstance(x,list): return clvm.list([to_sdk(i) for i in x])
    if isinstance(x,tuple): return clvm.pair(to_sdk(x[0]),to_sdk(x[1]))
    if x is None: return clvm.nil()
    if isinstance(x,int): return clvm.int(x)
    if isinstance(x,str): return clvm.atom(x.encode())
    return clvm.atom(x)
cases=[[],None,b"",[b""],[None],(1,(2,None)),[1,[2,[3]],("a",b"\x00")], ["flpcr-claim-v1",b"\x11"*32,b"\x22"*32,b"\x33"*32,2**70], (b"",b""), [0,-1,b"\x80"]]
for c in cases:
    if bytes(to_sdk(c).tree_hash())!=sha256tree(c): bad+=1; print("struct mismatch",c)
print("checked",len(vals),"ints,",len(cases),"structs; mismatches:",bad)
# note: b"" vs 0 vs None identical -> by CLVM design
print("0==None==b'':", sha256tree(0)==sha256tree(None)==sha256tree(b""), " list[] == None:", sha256tree([])==sha256tree(None))
