import cv2
import sys
import numpy as np

REFERENCE = "images/empty_reference_evening_rain.jpg"
TEST_IMAGE = sys.argv[1] if len(sys.argv) > 1 else "images/package_test_evening_rain.jpg"

# 白い壁際の置き配監視範囲
X1, Y1 = 430, 130
X2, Y2 = 610, 300

ref = cv2.imread(REFERENCE)
test = cv2.imread(TEST_IMAGE)

if ref is None or test is None:
    raise SystemExit("画像を読み込めません")

ref_roi = ref[Y1:Y2, X1:X2]
test_roi = test[Y1:Y2, X1:X2]

ref_gray = cv2.cvtColor(ref_roi, cv2.COLOR_BGR2GRAY)
test_gray = cv2.cvtColor(test_roi, cv2.COLOR_BGR2GRAY)

ref_gray = cv2.GaussianBlur(ref_gray, (9, 9), 0)
test_gray = cv2.GaussianBlur(test_gray, (9, 9), 0)

diff = cv2.absdiff(ref_gray, test_gray)
_, changed = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)

kernel = np.ones((5, 5), np.uint8)
changed = cv2.morphologyEx(changed, cv2.MORPH_OPEN, kernel)

changed_percent = (
    cv2.countNonZero(changed) / changed.size
) * 100

result = test.copy()
cv2.rectangle(result, (X1, Y1), (X2, Y2), (0, 255, 255), 2)

if changed_percent >= 4.0:
    status = "PACKAGE DETECTED"
    color = (0, 0, 255)
else:
    status = "NO PACKAGE"
    color = (0, 255, 0)

cv2.putText(
    result,
    f"{status} {changed_percent:.1f}%",
    (20, 35),
    cv2.FONT_HERSHEY_SIMPLEX,
    0.8,
    color,
    2,
)

cv2.imwrite("images/comparison_result.jpg", result)
cv2.imwrite("images/difference_mask.jpg", changed)

print(f"監視範囲の変化率: {changed_percent:.2f}%")
print(f"判定: {status}")
print("結果画像: images/comparison_result.jpg")
