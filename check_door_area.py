import sys
import cv2
import numpy as np

REFERENCE = "images/empty_reference_evening_rain.jpg"
TEST_IMAGE = sys.argv[1]

X1, Y1 = 500, 0
X2, Y2 = 640, 170

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

percent = cv2.countNonZero(changed) / changed.size * 100
print(f"扉確認領域の変化率: {percent:.2f}%")
