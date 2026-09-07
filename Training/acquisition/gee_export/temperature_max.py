import ee

# 添加认证步骤（首次运行时需要）
try:
    # 尝试直接初始化，如果已经认证过则不需要重新认证
    ee.Initialize(project='croy-floods')
except Exception as e:
    print("未检测到已认证的GEE账户，正在启动认证流程...")
    # 启动交互式认证
    ee.Authenticate()
    # 认证成功后初始化
    ee.Initialize(project='croy-floods')
    print("GEE账户认证成功！")

# 设置要下载的年份范围
start_year = 1984  # 起始年份
end_year = 1987    # 结束年份

# 1. 研究区域
roi = ee.FeatureCollection(
    'projects/croy-floods/assets/TibetanPlateau')

# 2. 加载 ERA5-Land 数据集 - 确保时间范围覆盖所有需要的年份
dataset = (
    ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR")
    .filterDate(f'{start_year}-01-01', f'{end_year+1}-01-01')  # 包含完整年份
    .filterBounds(roi)
)

# 3. 选择温度变量
temperature2m_max = dataset.select('temperature_2m_max')


# 4. 定义月度聚合函数（含单位转换：开尔文 → 摄氏度）
def create_monthly_image(year, month):
    start_date = ee.Date.fromYMD(year, month, 1)
    end_date = start_date.advance(1, 'month')

    # 当月每日温度集合
    monthly_collection = (
        temperature2m_max
        .filterDate(start_date, end_date)
        .map(lambda img: (
            img
            .subtract(273.15)  # 开尔文 → 摄氏度
            .clip(roi)
            .rename(ee.Date(
                img.get('system:time_start')).format(
                'YYYY-MM-dd'))
        ))
    )

    # 将每日影像拼成多波段
    return (
        monthly_collection.toBands()
        .set('year', year)
        .set('month', month)
    )


# 5. 生成多年度的年月列表
year_months = []
for year in range(start_year, end_year + 1):
    for month in range(1, 13):
        year_months.append((year, month))

print(f"已设置下载 {start_year} 至 {end_year} 年的月度温度最大值数据，共 {len(year_months)} 个月")

# 6. 在服务器端生成每个月的影像
monthly_images = ee.List([
    create_monthly_image(y, m) for (y, m) in year_months
])

# 7. 批量导出到 Google Drive
for i, (year, month) in enumerate(year_months):
    month_str = f"{month:02d}"
    image = ee.Image(monthly_images.get(i))

    task = ee.batch.Export.image.toDrive(
        image=image,
        description=f"temperature_2m_max_{year}{month_str}",
        fileNamePrefix=f"temperature_2m_max_{year}{month_str}",
        folder='temperature_2m_max_monthly',
        region=roi.geometry(),
        scale=1000,  # 分辨率（米）
        crs='EPSG:4326',
        maxPixels=1e13
    )
    task.start()
    print(
        f"已提交导出任务：temperature_2m_max_{year}{month_str}")

print("✅ 所有任务已提交至 GEE 任务队列。")
