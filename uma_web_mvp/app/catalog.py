STYLES = [
    {"key": "style_a", "name": "默认画风", "modes": ["txt2img", "img2img"]},
    {"key": "style_b", "name": "水彩新流", "modes": ["txt2img", "img2img"]},
    {"key": "anima", "name": "Anima二次元", "modes": ["txt2img", "img2img"], "hidden": True},
    {"key": "controlnet", "name": "ControlNet 控制", "modes": ["controlnet"]},
    {"key": "anima_owner", "name": "Anima 双采样", "modes": ["txt2img"]},
    {"key": "artist_chain_available", "name": "画师串 available", "modes": ["txt2img"], "allow_external_lora": False},
    {"key": "morialuluka", "name": "morialuluka", "modes": ["txt2img"], "allow_external_lora": False},
    {"key": "bridge_complete", "name": "大桥竣工", "modes": ["txt2img"], "allow_external_lora": False},
    {"key": "hayakawa_tazuna", "name": "手纲", "modes": ["txt2img"], "allow_external_lora": False},
    {"key": "akikawa_yayoi", "name": "理事长", "modes": ["txt2img"], "allow_external_lora": False},
    {"key": "loves_only_you", "name": "唯独爱你", "modes": ["txt2img", "img2img"], "character_key": "loves_only_you", "aliases": ["Loves Only You"], "allow_external_lora": False},
    {"key": "red_goddess", "name": "红女神", "modes": ["txt2img", "img2img"], "character_key": "red_goddess", "aliases": ["Darley Arabian"], "allow_external_lora": False},
    {"key": "blue_goddess", "name": "蓝女神", "modes": ["txt2img", "img2img"], "character_key": "blue_goddess", "aliases": ["Godolphin Barb"], "allow_external_lora": False},
    {"key": "b95", "name": "b95", "modes": ["txt2img", "img2img"], "character_key": "light_hello", "aliases": ["Light Hello"], "allow_external_lora": False},
    {"key": "haiseiko", "name": "海赛柯", "modes": ["txt2img", "img2img"], "character_key": "haiseiko", "aliases": ["Haiseiko"], "allow_external_lora": False},
    {"key": "mihono_bourbon", "name": "马机 (Mihono Bourbon)", "modes": ["txt2img", "img2img"], "character_key": "mihono_bourbon", "aliases": ["Mihono Bourbon"], "allow_external_lora": False},
    {"key": "wonder_acute", "name": "奶奶 (Wonder Acute)", "modes": ["txt2img", "img2img"], "character_key": "wonder_acute", "aliases": ["Wonder Acute"], "allow_external_lora": False},
    {"key": "verxina", "name": "极峰 (Verxina)", "modes": ["txt2img", "img2img"], "character_key": "verxina", "aliases": ["Verxina"], "allow_external_lora": False},
    {"key": "sakura_chiyono_o", "name": "牛牛 (Sakura Chiyono O)", "modes": ["txt2img", "img2img"], "character_key": "sakura_chiyono_o", "aliases": ["Sakura Chiyono O"], "allow_external_lora": False},
    {"key": "daiichi_ruby", "name": "红宝石 (Daiichi Ruby)", "modes": ["txt2img", "img2img"], "character_key": "daiichi_ruby", "aliases": ["Daiichi Ruby"], "allow_external_lora": False},
    {"key": "copano_rickey", "name": "小林 (Copano Rickey)", "modes": ["txt2img", "img2img"], "character_key": "copano_rickey", "aliases": ["Copano Rickey"], "allow_external_lora": False},
]

CONTROL_CHARACTERS = [
    {"key": "prompt", "name": "原文本（手动填写 prompt）"},
    {"key": "almond_eye", "name": "Almond Eye"},
    {"key": "wonder_acute", "name": "Wonder Acute"},
    {"key": "chrono_genesis", "name": "Chrono Genesis"},
    {"key": "hishi_miracle", "name": "Hishi Miracle"},
    {"key": "sakura_chiyono_o", "name": "Sakura Chiyono O"},
    {"key": "forever_young", "name": "Forever Young"},
    {"key": "loves_only_you", "name": "Loves Only You"},
    {"key": "akikawa_yayoi", "name": "Akikawa Yayoi"},
    {"key": "misaka_mikoto", "name": "Misaka Mikoto"},
    {"key": "matsumae_ohana", "name": "Matsumae Ohana"},
    {"key": "yamanin_zephyr", "name": "Yamanin Zephyr"},
    {"key": "eishin_flash", "name": "Eishin Flash"},
    {"key": "curren_chan", "name": "Curren Chan"},
    {"key": "curren_bouquetdor", "name": "Curren Bouquet d'Or"},
]

STYLE_BY_KEY = {item["key"]: item for item in STYLES}
CONTROL_CHARACTER_KEYS = {item["key"] for item in CONTROL_CHARACTERS}
