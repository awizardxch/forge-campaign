import os, hashlib
from flpcr.merkle import build_tree, root_from_proof
H=lambda b: hashlib.sha256(b).digest()
def rue_simplify(leaf, path, hashes):  # transcription of merkle_utils.rue
    h=H(b"\x01"+leaf)
    for s in hashes:
        h = H(b"\x02"+s+h) if path&1 else H(b"\x02"+h+s)
        path >>= 1
    return h
# chia merkle_utils.py reference (transcribed): list_to_binary_tree midpoint=(n+1)>>1
def chia_root(objs):
    if len(objs)==1: return H(b"\x01"+objs[0])
    m=(len(objs)+1)>>1
    return H(b"\x02"+chia_root(objs[:m])+chia_root(objs[m:]))
bad=0
for n in list(range(1,70))+[255,256,257]:
    leaves=[os.urandom(32) for _ in range(n)]
    root,proofs=build_tree(leaves)
    if root!=chia_root(leaves): bad+=1
    for l in leaves:
        p,hs=proofs[l]
        if rue_simplify(l,p,hs)!=root or root_from_proof(l,(p,hs))!=root: bad+=1
        # node-as-leaf: an internal node can't verify as a leaf because of the 0x01 wrap
    if n>=2:
        # try every internal node at depth-1 as a fake leaf with a truncated proof
        l=leaves[0]; p,hs=proofs[l]
        acc=H(b"\x01"+l)
        for i,s in enumerate(hs[:-1]):
            acc = H(b"\x02"+s+acc) if (p>>i)&1 else H(b"\x02"+acc+s)
            if rue_simplify(acc,p>>(i+1),hs[i+1:])==root: bad+=1; print("node accepted as leaf!")
print("mismatches/forgeries:",bad)
