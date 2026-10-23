import os
from PIL import Image, ImageOps
for f in os.listdir('photos'):
    p=os.path.join('photos',f)
    try:
        im=ImageOps.exif_transpose(Image.open(p)).convert('RGB'); im.thumbnail((520,520)); im.save(p,'JPEG',quality=74)
    except Exception as e:
        print('bad',f,e); os.remove(p)
