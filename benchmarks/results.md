# Token benchmark results (issue #26)

- Run date: 2026-08-06
- Overture release: 2026-07-22.0
- Tasks: 30
- Accuracy: 27/30 (90.0%)
- Median tokens ratio (raw / placeroot), over 30 scored tasks: 2543.0x
- Mean tokens ratio: 11246.6x

Every task that ran is in the table below, including failures — nothing is dropped from the aggregate.

## Per-task results

| task | category | correct | placeroot tokens | raw tokens | ratio |
|---|---|---|---:|---:|---:|
| austin_texas_capitol | point_in_admin | yes | 90 | 2349639 | 26107.1x |
| brooklyn_borough_hall | point_in_admin | yes | 113 | 1819534 | 16102.1x |
| la_city_hall | point_in_admin | yes | 94 | 1880981 | 20010.4x |
| chicago_willis_tower | point_in_admin | yes | 135 | 1806458 | 13381.2x |
| seattle_space_needle | point_in_admin | yes | 91 | 1807274 | 19860.2x |
| miami_downtown | point_in_admin | yes | 92 | 2000858 | 21748.5x |
| denver_state_capitol | point_in_admin | yes | 93 | 1752816 | 18847.5x |
| times_square_restaurant | within_distance | yes | 71 | 766290 | 10792.8x |
| chicago_loop_coffee | within_distance | yes | 75 | 42024 | 560.3x |
| hollywood_highland_restaurant | within_distance | yes | 74 | 167766 | 2267.1x |
| eiffel_tower_restaurant | within_distance | yes | 69 | 153342 | 2222.3x |
| yellowstone_lake_grocery | within_distance | yes | 13 | 0 | 0.0x |
| pacific_ocean_any_place | within_distance | yes | 13 | 0 | 0.0x |
| sahara_desert_any_place | within_distance | yes | 13 | 0 | 0.0x |
| grand_canyon_remote_restaurant | within_distance | yes | 13 | 0 | 0.0x |
| times_square_coffee | nearest_poi | **NO** | 181 | 40841 | 225.6x |
| chicago_loop_coffee | nearest_poi | yes | 185 | 20348 | 110.0x |
| seattle_downtown_coffee | nearest_poi | yes | 183 | 35743 | 195.3x |
| sf_union_square_coffee | nearest_poi | **NO** | 181 | 23338 | 128.9x |
| la_downtown_coffee | nearest_poi | **NO** | 190 | 20250 | 106.6x |
| boston_downtown_coffee | nearest_poi | yes | 183 | 28713 | 156.9x |
| manhattan_vs_rural_upstate_ny | area_comparison | yes | 526 | 17410408 | 33099.6x |
| chicago_loop_vs_rural_il | area_comparison | yes | 526 | 5454757 | 10370.3x |
| sf_union_sq_vs_sierra_backcountry | area_comparison | yes | 514 | 5916508 | 11510.7x |
| austin_6th_st_vs_hill_country | area_comparison | yes | 491 | 2550036 | 5193.6x |
| times_square_walk_15min | isochrone | yes | 2044 | 2836060 | 1387.5x |
| times_square_drive_ge_walk_15min | isochrone | yes | 3325 | 239697321 | 72089.4x |
| chicago_loop_walk_10min_vs_20min | isochrone | yes | 3371 | 4687640 | 1390.6x |
| sf_union_sq_cycle_ge_walk_15min | isochrone | yes | 3770 | 10627117 | 2818.9x |
| austin_downtown_drive_15min | isochrone | yes | 2039 | 95251683 | 46714.9x |

## Failure detail

- **times_square_coffee** (nearest_poi): expected one of ['Starbucks'] in top-3, got ['Joe Coffee Company', "Dunkin'", 'Central Perk']
- **sf_union_square_coffee** (nearest_poi): expected one of ['Starbucks'] in top-3, got ['Bancarella', 'Lux Cafe Club', 'Hyatt Coffee Bar']
- **la_downtown_coffee** (nearest_poi): expected one of ['Starbucks'] in top-3, got ['Barista Society Coffee Boutique', 'Aquarela Coffee', 'The Coffee Bean & Tea Leaf']
