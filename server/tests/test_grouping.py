from phylactere.grouping import group_lines, mark_edges, reading_order
from phylactere.schemas import Box
from tests.helpers import GROUPING, line


def texts(bubbles):
    return [b.text for b in bubbles]


def test_no_lines():
    assert group_lines([], GROUPING) == []


def test_centered_lines_of_one_bubble_are_merged():
    lines = [
        line(100, 100, 200, text="너무 소리치지 마"),
        line(90, 135, 220, text="그러다 크리쳐라도"),
        line(95, 170, 210, text="나오면 어떡하려고"),
    ]
    bubbles = group_lines(lines, GROUPING)

    assert texts(bubbles) == ["너무 소리치지 마 그러다 크리쳐라도 나오면 어떡하려고"]
    assert bubbles[0].box == Box(x=90, y=100, w=220, h=100)


def test_lines_far_apart_vertically_are_separate():
    # écart de 40 px > 0,8 × 30 px
    bubbles = group_lines([line(100, 100, 200), line(100, 170, 200)], GROUPING)
    assert len(bubbles) == 2


def test_vertical_gap_just_under_threshold_merges():
    # écart de 23 px < 0,8 × 30 = 24 px
    bubbles = group_lines([line(100, 100, 200), line(100, 153, 200)], GROUPING)
    assert len(bubbles) == 1


def test_side_by_side_bubbles_at_same_height_are_separate():
    bubbles = group_lines(
        [line(50, 100, 150, text="왼쪽"), line(450, 100, 150, text="오른쪽")], GROUPING
    )
    assert texts(bubbles) == ["왼쪽", "오른쪽"]


def test_small_overlap_but_close_centers_merges():
    # Ligne courte centrée sous une ligne longue : recouvrement total de la plus étroite.
    bubbles = group_lines([line(100, 100, 300), line(220, 135, 60)], GROUPING)
    assert len(bubbles) == 1


def test_weak_overlap_and_distant_centers_are_separate():
    # Recouvrement de 20 px (< 30 % de 100) et centres distants de 90 px (> 60 px).
    bubbles = group_lines([line(100, 100, 100), line(180, 135, 100)], GROUPING)
    assert len(bubbles) == 2


def test_overlap_threshold_uses_narrower_line():
    # Recouvrement de 30 px = 30 % de la ligne étroite (100 px), mais 7,5 % de la large.
    bubbles = group_lines([line(0, 100, 400), line(370, 135, 100)], GROUPING)
    assert len(bubbles) == 1


def test_grouping_is_transitive():
    # a-b et b-c sont proches, a-c ne l'est pas : tout finit dans la même bulle.
    lines = [line(100, 100, 200), line(100, 135, 200), line(100, 170, 200)]
    assert len(group_lines(lines, GROUPING)) == 1


def test_bubbles_are_sorted_in_reading_order():
    lines = [
        line(400, 500, 100, text="셋"),
        line(100, 100, 100, text="하나"),
        line(400, 100, 100, text="둘"),
    ]
    assert texts(group_lines(lines, GROUPING)) == ["하나", "둘", "셋"]


def test_reading_order_within_a_row_goes_left_to_right():
    lines = [
        line(300, 102, 80, text="B"),
        line(100, 100, 80, text="A"),
        line(100, 140, 80, text="C"),
    ]
    assert [item.text for item in reading_order(lines)] == ["A", "B", "C"]


def test_thresholds_come_from_config():
    strict = GROUPING.model_copy(update={"max_vertical_gap": 0.1})
    assert len(group_lines([line(100, 100, 200), line(100, 135, 200)], strict)) == 2


def test_mark_edges():
    bubbles = group_lines(
        [
            line(100, 3, 100, text="위"),
            line(100, 800, 100, text="가운데"),
            line(100, 1565, 100, text="아래"),
        ],
        GROUPING,
    )
    mark_edges(bubbles, image_height=1600, margin=8)
    assert [b.edges for b in bubbles] == [["top"], [], ["bottom"]]


def test_bubble_spanning_whole_image_touches_both_edges():
    bubbles = mark_edges(
        group_lines([line(0, 0, 100, h=100)], GROUPING), image_height=100, margin=0
    )
    assert bubbles[0].edges == ["top", "bottom"]
