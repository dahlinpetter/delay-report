"""Generate an EXAMPLE delays.json (3 weeks of fake Öresundståg traffic) for the artifact."""
import json, random, sys
from datetime import date, timedelta
STOPS = ["Lund C","Malmö C","Triangeln","Hyllie","Kastrup","Ørestad","København H"]
OFF = [0,13,17,21,33,36,42]
rng = random.Random(11)
end = date(2026,10,16)
trains=[]; n=0
for i in range(20,-1,-1):
    d = end - timedelta(days=i)
    bad_day = rng.random() < .25   # some days have an incident
    for direction in (1,-1):
        order = list(range(7)) if direction==1 else list(range(6,-1,-1))
        offs = OFF if direction==1 else [42-o for o in OFF[::-1]]
        for start in list(range(5*60+2, 10*60, 20)) + list(range(14*60+2, 20*60, 20)):
            n+=1
            r = rng.random()
            base = 0 if r<.7 else rng.choice([2,3,5,8,12])
            if bad_day and rng.random()<.45: base = rng.choice([18,24,31,44,52,67])
            elif rng.random()<.05: base = rng.choice([21,27,41,62])
            canc = rng.random() < (.03 if bad_day else .006)
            stops=[]; dly=base*0.6
            for k,(si,o) in enumerate(zip(order,offs)):
                if k: dly = max(0, dly + rng.choice([0,0,1,2,-1]) + (base*0.4/6))
                stops.append([si, start+o, round(dly)])
            trains.append({"d":d.isoformat(),"n":1000+n%900,"s":stops,"c":int(canc)})
json.dump({"example":True,"updated":f"{end.isoformat()}T18:30","stops":STOPS,"trains":trains},
          open(sys.argv[1],"w"),separators=(",",":"),ensure_ascii=False)
print(len(trains),"trains")
