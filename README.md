# Price Controls & Surplus: explainer video

`price_controls_surplus.mp4` is a 1:45, 1080p, silent video with on-screen captions. It shows how a binding
**price ceiling** and a binding **price floor** change consumer surplus (CS), producer surplus (PS), and create
**deadweight loss** (DWL). The triangles, trapezoids and transfer rectangles are drawn and animated on a
supply-and-demand graph, next to a live "surplus scorecard".

## The model

All areas are round numbers so the arithmetic is easy to follow:

| | Demand `P = 100 − Q`, Supply `P = 10 + 0.5Q` |
|---|---|
| Equilibrium | Q* = 60, P* = $40 · CS = $1,800 (triangle) · PS = $900 (triangle) · Total = $2,700 |
| Ceiling at $30 | Qs = 40, Qd = 70 → shortage 30 · CS = $2,000 (**trapezoid**) · PS = $400 (triangle) · DWL = $300 |
| Floor at $60 | Qd = 40, Qs = 100 → excess supply 60 · CS = $800 (triangle) · PS = $1,600 (**trapezoid**) · DWL = $300 |

Walkthrough of each control:

1. **Transfer rectangle.** The rectangle between P* and the controlled price, over the 40 units still traded, moves
   between groups: $400 from producers to consumers under the ceiling, and $800 from consumers to producers under
   the floor.
2. **Deadweight loss.** The triangle between demand and supply from Q = 40 to 60 is surplus nobody gets ($300).
   It is $200 of lost CS plus $100 of lost PS.
3. **Resulting shapes.** The side the control favours ends up with a trapezoid; the other side keeps a smaller
   triangle.

Assumptions (stated on screen):

- Under the ceiling, the scarce units go to the buyers who value them most.
- Under the floor, only the 40 units demanded get produced, by the lowest-cost sellers.

## Re-rendering

Requires Python 3 with Pillow, plus ffmpeg built with libx264.

```sh
python3 make_video.py                      # -> price_controls_surplus.mp4 (~3 min on 4 cores)
python3 make_video.py --stills 20,45,80    # PNG frames at those timestamps, for quick checks
```

Scene timings, captions and the model all live in `make_video.py`. `check_model()` asserts that every shaded
polygon's area matches the numbers shown on screen.
