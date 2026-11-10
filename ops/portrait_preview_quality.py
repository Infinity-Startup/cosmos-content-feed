"""Select a high-resolution single-character portrait; never use a whole page fallback."""
import io
import hashlib
import os
import re
from pathlib import Path
import threading

import cv2
import numpy as np
import onnxruntime as ort
from PIL import Image, ImageOps

OUTPUT_SIZE = 384
CROP_VERSION = 5
_local = threading.local()


def crop_bounds(face, size, scale=1.3, dx=0, dy=0, minimum=256):
    x, y, w, h, confidence = face
    side = max(minimum, round(max(w, h) * scale))
    width, height = size
    if side > min(width, height) or w / side < .42:
        return None
    left = round(x + w * (.5 + dx) - side / 2)
    top = round(y + h * (.5 + dy) - side / 2)
    left = min(max(0, left), width - side)
    top = min(max(0, top), height - side)
    return left, top, left + side, top + side


def visual_quality(crop, face_in_crop):
    """Reject gutters, panel separators, and white bubble-like regions outside the face."""
    rgb = np.asarray(crop.resize((256, 256)))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    white = ((gray > 238) & (hsv[:, :, 1] < 28)).astype(np.uint8)
    x, y, w, h = face_in_crop
    known_face = np.zeros((256, 256), np.uint8)
    cv2.rectangle(known_face, (max(0, int(x * 256)), max(0, int(y * 256))), (min(255, int((x + w) * 256)), min(255, int((y + h) * 256))), 1, -1)
    outside_white = white * (1 - known_face)
    whitespace = float(outside_white.mean())
    if whitespace > .055:
        return None
    count, labels, stats, centers = cv2.connectedComponentsWithStats(outside_white)
    bubble = max((int(row[cv2.CC_STAT_AREA]) / 65536 for row in stats[1:]), default=0)
    if bubble > .018:
        return None
    # Long, flat strips at an edge are gutters or frame separators, not portraits.
    for strip in [gray[:8], gray[-8:], gray[:, :8].T, gray[:, -8:].T]:
        if np.any((strip > 242).mean(axis=1) > .9) or np.any((strip < 8).mean(axis=1) > .97):
            return None
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if sharpness < 250:
        return None
    return {'outsideFaceWhiteFraction': round(whitespace, 5), 'largestWhiteRegion': round(bubble, 5), 'sharpness': round(sharpness, 2)}


def recognize_text(image):
    import objc
    import Vision
    from Foundation import NSData
    with objc.autorelease_pool():
        output = io.BytesIO()
        image.save(output, 'PNG')
        body = output.getvalue()
        data = NSData.dataWithBytes_length_(body, len(body))
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(False)
        request.setRecognitionLanguages_(['ko-KR', 'en-US', 'ja-JP', 'zh-Hans'])
        request.setAutomaticallyDetectsLanguage_(True)
        request.setMinimumTextHeight_(.015)
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, {})
        geometry = Vision.VNDetectTextRectanglesRequest.alloc().init()
        ok, error = handler.performRequests_error_([request, geometry], None)
        if not ok:
            raise RuntimeError('Text screening unavailable; refusing to publish an unchecked crop')
        result = []
        for observation in request.results() or []:
            candidate = observation.topCandidates_(1)[0]
            if candidate.confidence() >= .25 and candidate.string().strip():
                rect = observation.boundingBox()
                result.append((float(rect.origin.x), float(1 - rect.origin.y - rect.size.height), float(rect.size.width), float(rect.size.height)))
        for observation in geometry.results() or []:
            rect = observation.boundingBox()
            result.append((float(rect.origin.x), float(1 - rect.origin.y - rect.size.height), float(rect.size.width), float(rect.size.height)))
        return result


def fast_latin_text(image):
    """Cheap watermark/English-caption rejection before mandatory visual review."""
    import objc
    import Vision
    from Foundation import NSData
    with objc.autorelease_pool():
        output = io.BytesIO()
        image.save(output, 'PNG')
        body = output.getvalue()
        data = NSData.dataWithBytes_length_(body, len(body))
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelFast)
        request.setRecognitionLanguages_(['en-US'])
        request.setUsesLanguageCorrection_(False)
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, {})
        ok, error = handler.performRequests_error_([request], None)
        if not ok:
            raise RuntimeError('Watermark screening unavailable')
        return any(candidate.confidence() >= .3 and re.search(r'[A-Za-z0-9]{3,}', candidate.string()) for observation in request.results() or [] for candidate in observation.topCandidates_(1))


def face_hash(image, face):
    x, y, w, h = face[:4]
    crop = image.crop((round(x), round(y), round(x + w), round(y + h))).convert('L').resize((32, 32))
    dct = cv2.dct(np.asarray(crop, dtype=np.float32))[:8, :8].flatten()
    bits = dct > np.median(dct[1:])
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


class PortraitSelector:
    def __init__(self, model):
        cv2.setNumThreads(1)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        for key, value in [('batch', 1), ('height', 512), ('width', 512)]:
            options.add_free_dimension_override_by_name(key, value)
        providers = ['CoreMLExecutionProvider', 'CPUExecutionProvider'] if 'CoreMLExecutionProvider' in ort.get_available_providers() else ['CPUExecutionProvider']
        self.session = ort.InferenceSession(str(model), sess_options=options, providers=providers)

    def faces(self, tile, top):
        array = np.asarray(tile)
        height, width = array.shape[:2]
        scale = 512 / max(width, height)
        resized = cv2.resize(array, (round(width * scale), round(height * scale)))
        canvas = np.full((512, 512, 3), 114, np.uint8)
        canvas[:resized.shape[0], :resized.shape[1]] = resized
        blob = canvas.transpose(2, 0, 1)[None].astype(np.float32) / 255
        prediction = self.session.run(None, {'images': blob})[0][0].T
        hits = prediction[prediction[:, 4] >= .65]
        boxes = [[float((x - w / 2) / scale), float((y - h / 2) / scale), float(w / scale), float(h / scale)] for x, y, w, h, score in hits]
        scores = [float(hit[4]) for hit in hits]
        indexes = cv2.dnn.NMSBoxes(boxes, scores, .65, .4)
        return [(*boxes[int(index)][:2], boxes[int(index)][2], boxes[int(index)][3], scores[int(index)]) for index in np.asarray(indexes).flatten() if boxes[int(index)][1] > 8 and boxes[int(index)][1] + boxes[int(index)][3] < height - 8]

    def reference_hashes(self, body):
        with Image.open(io.BytesIO(body)) as opened:
            image = opened.convert('RGB')
        side = min(image.size)
        hashes = []
        for top in range(0, max(1, image.height - side + 1), max(1, side // 2)):
            for x, y, w, h, score in self.faces(image.crop((0, top, image.width, top + side)), top):
                if score >= .7:
                    hashes.append(face_hash(image, (x, y + top, w, h)))
        return hashes

    def select(self, body, excluded_hashes=(), blocked_regions=(), editorial=False, full_scan=False, min_face=140, min_crop=256, excluded_crops=(), tight=False):
        with Image.open(io.BytesIO(body)) as opened:
            if opened.width * opened.height > 60_000_000:
                raise ValueError('Source image too large')
            image = ImageOps.exif_transpose(opened).convert('RGB')
        width, height = image.size
        tile_side = min(width, height)
        scan_height = height if full_scan else min(height, width * 12)
        tops = list(range(0, max(1, scan_height - tile_side + 1), max(1, tile_side * 3 // 4)))
        if tops[-1] != scan_height - tile_side:
            tops.append(scan_height - tile_side)
        all_faces = []
        for top in tops:
            local_faces = self.faces(image.crop((0, top, width, top + tile_side)), top)
            all_faces.extend((x, y + top, w, h, score) for x, y, w, h, score in local_faces)
        if not all_faces:
            return None
        indexes = cv2.dnn.NMSBoxes([list(face[:4]) for face in all_faces], [face[4] for face in all_faces], .7, .4)
        faces = [all_faces[int(index)] for index in np.asarray(indexes).flatten()]
        seen = []
        for _ in [None]:
            for face in sorted(faces, key=lambda item: item[2] * item[3] * item[4], reverse=True):
                x, y, w, h, confidence = face
                if max(w, h) < min_face or confidence < (.7 if editorial else .85):
                    continue
                perceptual = face_hash(image, face)
                if any((perceptual ^ value).bit_count() <= 10 for value in excluded_hashes):
                    continue
                signature = (round(x / 30), round(y / 30))
                if signature in seen:
                    continue
                seen.append(signature)
                context_side = max(256, round(max(w, h) * 1.55))
                cx, cy = x + w / 2, y + h / 2
                context_box = (max(0, round(cx - context_side)), max(0, round(cy - context_side)), min(width, round(cx + context_side)), min(height, round(cy + context_side)))
                context = image.crop(context_box)
                context.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
                detected_text = [] if editorial else recognize_text(context)
                text_boxes = [(context_box[0] + tx * (context_box[2] - context_box[0]), context_box[1] + ty * (context_box[3] - context_box[1]), tw * (context_box[2] - context_box[0]), th * (context_box[3] - context_box[1])) for tx, ty, tw, th in detected_text]
                framings = [(1.03, .07), (1.03, -.07), (1.1, 0), (1.2, 0)] if tight else [(1.3, 0), (1.42, 0), (1.55, 0), (1.15, 0)]
                for framing, vertical in framings:
                    for shift in [0, -.09, .09]:
                        bounds = crop_bounds(face, image.size, framing, shift, dy=vertical, minimum=min_crop)
                        if not bounds:
                            continue
                        left, y0, right, bottom = bounds
                        if any(bx < right and bx + bw > left and by < bottom and by + bh > y0 for bx, by, bw, bh in blocked_regions):
                            continue
                        side = right - left
                        if any(other is not face and other[4] >= .7 and left < other[0] + other[2] / 2 < right and y0 < other[1] + other[3] / 2 < bottom for other in faces):
                            continue
                        if any(tx < right + 5 and tx + tw > left - 5 and ty < bottom + 5 and ty + th > y0 - 5 for tx, ty, tw, th in text_boxes):
                            continue
                        crop = image.crop(bounds)
                        relative = ((x - left) / side, (y - y0) / side, w / side, h / side)
                        quality = visual_quality(crop, relative)
                        if not quality:
                            continue
                        preview = crop.resize((OUTPUT_SIZE, OUTPUT_SIZE), Image.Resampling.LANCZOS)
                        if editorial and fast_latin_text(preview):
                            continue
                        output = io.BytesIO()
                        preview.save(output, 'WEBP', quality=94, method=6)
                        if hashlib.sha256(output.getvalue()).hexdigest() in excluded_crops:
                            continue
                        report = {'version': CROP_VERSION, 'width': OUTPUT_SIZE, 'height': OUTPUT_SIZE, 'textRegions': None if editorial else 0, 'textScreening': 'awaiting-editorial-review' if editorial else 'multilingual-context-and-regions', 'faceConfidence': round(float(confidence), 4), 'faceHash': f'{perceptual:016x}', 'recognitionLanguages': [] if editorial else ['ko-KR', 'en-US', 'ja-JP', 'zh-Hans'], 'sourceCrop': list(bounds), 'sourceSize': [width, height], 'detectedFace': [round(float(value), 3) for value in face[:4]], **quality}
                        return output.getvalue(), report
        return None


def selector(model):
    if not hasattr(_local, 'selector'):
        _local.selector = PortraitSelector(model)
    return _local.selector
