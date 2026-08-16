from paddleocr import PaddleOCR

ocr = PaddleOCR(lang="en", enable_mkldnn=False, use_doc_orientation_classify=True,
    use_doc_unwarping=True,
    use_textline_orientation=True,text_det_limit_side_len=4032,   # match (or exceed) your actual image size
    text_det_limit_type="max",text_det_thresh=0.2,        # default ~0.3 — lower = more sensitive to faint strokes
    text_det_box_thresh=0.5,    # default ~0.6 — lower = keeps more borderline detections
    text_det_unclip_ratio=2.0, )  # swap "korean" for "en", "ch", etc. as needed
results = ocr.predict("C:\\Users\\yuddu\\Desktop\\cs\\projects\\comictranslator\\ocrtest\\networks.jpg")

for res in results:
    for text, score, box in zip(res["rec_texts"], res["rec_scores"], res["rec_boxes"]):
        print(f"{score:.2f}  {text}  {box}")